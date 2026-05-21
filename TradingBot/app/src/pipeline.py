"""End-to-end capture → analyze → store pipeline. Headless — no input() prompts.

Used by main.py (terminal flow) and Trade_Perf's FastAPI signals.py
routes (Snip & Analyze button + NinjaScript-triggered /api/capture-from-nt
endpoint). When NinjaScript provides a market_context payload, it's
prepended to the prompt as authoritative price data and stored on the
resulting signal record.

The cross-signal LLM reconciliation step ("Reconciliations from this
analysis" card in the dashboard) was removed 2026-05-19; outcomes are
managed per-signal via the outcome_watcher and the Signal Detail UI.

Returns the enriched record (with timestamp) so callers can route to it.
"""
import logging
from pathlib import Path
from typing import Any

from . import proposal_sanity
from .local_llm_analyzer import MODEL, analyze_with_floor
from .screenshot_capturer import capture_via_snip
from .signal_storage import append_signal

logger = logging.getLogger(__name__)


def run_pipeline(
    screenshots_dir: Path,
    signals_log: Path,
    prompt: str,
    market_context: dict[str, Any] | None = None,
    image_path: Path | None = None,
) -> dict:
    """Optionally capture from clipboard → inject market context → call vision LLM → append to JSONL → reconcile.

    If ``image_path`` is provided (e.g. NS embedded the chart bitmap in the
    POST), the pipeline uses it verbatim and skips the Windows Snipping
    overlay. This is the preferred path -- the Session-0 isolation of the
    NSSM-hosted uvicorn breaks cross-session URI activation of the snip tool.

    If ``image_path`` is None, falls back to ``capture_via_snip`` which opens
    the Snipping overlay. Used by the standalone CLI (``main.py``) and as
    legacy compatibility for older HelmAnalyzer builds.

    Returns the enriched signal record (with `timestamp` and `proposal`).
    Raises RuntimeError if no image arrives (snip cancelled / timed out).
    """
    src = "pre-captured" if image_path else "snip"
    logger.info("Pipeline starting (context=%s, image=%s)",
                "yes" if market_context else "no", src)
    if image_path is None:
        image_path = capture_via_snip(screenshots_dir)

    full_prompt = prompt
    if market_context:
        full_prompt = _format_context_for_prompt(market_context) + "\n\n---\n\n" + prompt

    result = analyze_with_floor(image_path, full_prompt)

    # Sanity-check the LLM's proposed prices against the latest reference in
    # feed.db. Catches hallucinations where the model misread the price axis
    # and emitted entry/stop/target dozens of percent off the instrument's
    # actual range. If invalid, persist with deleted=True + auto_dismissed
    # flags so the record exists for audit but is hidden from the dashboard
    # and won't trigger outcome-watcher rescans.
    is_valid, sanity_reason = proposal_sanity.sanity_check(result["proposal"])

    record_payload = {
        "screenshot_path": str(image_path),
        "proposal": result["proposal"],
        "raw_response": result["raw_response"],
        "duration_s": result["duration_s"],
        "model": MODEL,
        "market_context": market_context,
    }

    # Lift the ATM's total contract count onto the top-level record so the
    # metrics calculator and the dashboard W/L rollup size correctly for
    # scale-out templates (2c TP1 + runner, etc.). Default DEFAULT_POSITION_SIZE
    # from signal_storage handles the no-ATM case; user can still override
    # via the Signal Detail "Contracts / Shares" field.
    atm_qty = result["proposal"].get("atm_total_qty")
    if isinstance(atm_qty, int) and atm_qty > 0:
        record_payload["position_size"] = float(atm_qty)
    if not is_valid:
        record_payload["deleted"]               = True
        record_payload["auto_dismissed"]        = True
        record_payload["auto_dismissed_reason"] = sanity_reason
        logger.warning(
            "Auto-dismissing proposal at capture time: %s", sanity_reason)

    record = append_signal(signals_log, record_payload)
    logger.info("Capture complete: timestamp=%s%s",
                record["timestamp"], " (auto-dismissed)" if not is_valid else "")

    return record


def _format_context_for_prompt(ctx: dict) -> str:
    """Turn the NS context dict into a text block we prepend to the analyzer prompt.

    Goal: give the model authoritative numbers for prices/levels so it doesn't
    have to read them off the chart axis. Image stays the source of structural
    interpretation; this block is the source of truth for prices.
    """
    lines = [
        "## Authoritative Market Context (from NinjaTrader)",
        f"Instrument: {ctx.get('instrument', 'unknown')}",
    ]
    current = ctx.get("current") or {}
    if current:
        lines.append(
            f"Current: bid={current.get('bid')}, ask={current.get('ask')}, last={current.get('last')}"
        )

    tfs = ctx.get("timeframes") or {}
    if tfs:
        lines.append("")
        lines.append("Timeframes:")
        for tf_name, tf_data in tfs.items():
            if not isinstance(tf_data, dict):
                continue
            parts = ", ".join(f"{k}={v}" for k, v in tf_data.items() if v is not None)
            lines.append(f"  {tf_name}: {parts}")

    daily = ctx.get("daily_levels") or {}
    if daily:
        lines.append("")
        lines.append("Daily levels:")
        if "pivot_p" in daily:
            lines.append(
                f"  Floor pivots: P={daily.get('pivot_p')}, "
                f"R1={daily.get('pivot_r1')}, R2={daily.get('pivot_r2')}, R3={daily.get('pivot_r3')}, "
                f"S1={daily.get('pivot_s1')}, S2={daily.get('pivot_s2')}, S3={daily.get('pivot_s3')}"
            )
        if "today_high" in daily:
            lines.append(f"  Today: high={daily.get('today_high')}, low={daily.get('today_low')}")
        if "yesterday_high" in daily:
            lines.append(
                f"  Yesterday: high={daily.get('yesterday_high')}, "
                f"low={daily.get('yesterday_low')}, close={daily.get('yesterday_close')}"
            )

    lines.append("")
    lines.append(
        "Use the prices above as authoritative — do not re-read them from the chart axis. "
        "Use the chart screenshot for structural interpretation (trend, pullbacks, support/resistance) only."
    )
    return "\n".join(lines)


