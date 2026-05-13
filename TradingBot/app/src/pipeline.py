"""End-to-end capture → analyze → store pipeline. Headless — no input() prompts.

Used by main.py (terminal flow) and Trade_Perf's FastAPI signals.py
routes (Snip & Analyze button + NinjaScript-triggered /api/capture-from-nt
endpoint). When NinjaScript provides a market_context payload, it's
prepended to the prompt as authoritative price data and stored on the
resulting signal record.

Returns the enriched record (with timestamp) so callers can route to it.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from . import proposal_sanity
from .instruments import normalize_symbol
from .local_llm_analyzer import MODEL, analyze_with_floor, reconcile
from .screenshot_capturer import capture_via_snip
from .signal_storage import append_signal, append_update, load_all

logger = logging.getLogger(__name__)

MAX_RECONCILE_TARGETS = 3  # most recent N open trades on same instrument


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
    if not is_valid:
        record_payload["deleted"]               = True
        record_payload["auto_dismissed"]        = True
        record_payload["auto_dismissed_reason"] = sanity_reason
        logger.warning(
            "Auto-dismissing proposal at capture time: %s", sanity_reason)

    record = append_signal(signals_log, record_payload)
    logger.info("Capture complete: timestamp=%s%s",
                record["timestamp"], " (auto-dismissed)" if not is_valid else "")

    if is_valid:
        try:
            _reconcile_open_trades(signals_log, image_path, record)
        except Exception:
            logger.exception("Reconciliation step failed (non-fatal)")

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


def _reconcile_open_trades(signals_log: Path, image_path: Path, new_record: dict) -> None:
    """For each open trade on the same instrument, ask the LLM if it resolved.

    Stores verdicts as `outcome_suggestion` updates on the prior signals.
    Does not modify outcome directly — user confirms/rejects in the dashboard.
    """
    new_root = normalize_symbol(new_record.get("proposal", {}).get("instrument", ""))
    if not new_root:
        return

    open_trades = _find_open_trades(signals_log, new_root, exclude_ts=new_record["timestamp"])
    if not open_trades:
        logger.info("No open trades on %s to reconcile", new_root)
        return

    logger.info("Reconciling %d open trade(s) on %s", len(open_trades), new_root)
    for prior in open_trades[:MAX_RECONCILE_TARGETS]:
        try:
            verdict = reconcile(image_path, prior["proposal"])
        except Exception:
            logger.exception("Reconciliation call failed for %s", prior["timestamp"])
            continue

        result = verdict.get("result")
        if result not in ("target", "stop", "no_fill", "breakeven"):
            logger.info(
                "Reconciliation for %s = %s (no suggestion stored)",
                prior["timestamp"], result,
            )
            continue

        suggestion = {
            "result": result,
            "confidence": verdict.get("confidence", 0.0),
            "reasoning": verdict.get("reasoning", ""),
            "suggested_at": datetime.now().isoformat(timespec="seconds"),
            "source_signal_ts": new_record["timestamp"],
        }
        append_update(signals_log, prior["timestamp"], outcome_suggestion=suggestion)
        # Clear any prior dismissal so the new suggestion is visible
        append_update(signals_log, prior["timestamp"], outcome_suggestion_dismissed=False)
        logger.info(
            "Suggested %s for %s (conf=%.2f)",
            result, prior["timestamp"], suggestion["confidence"],
        )


def _find_open_trades(signals_log: Path, instrument_root: str, exclude_ts: str) -> list[dict]:
    """Return signals on this instrument with no outcome.result, sorted newest-first."""
    signals = load_all(signals_log)
    open_trades = []
    for ts, rec in signals.items():
        if ts == exclude_ts:
            continue
        if rec.get("deleted"):
            continue
        # 'pending' is the new default — still treat as open for reconciliation.
        _result = (rec.get("outcome") or {}).get("result")
        if _result and _result != "pending":
            continue
        prop = rec.get("proposal") or {}
        if normalize_symbol(prop.get("instrument", "")) != instrument_root:
            continue
        if prop.get("direction") == "flat":
            continue  # nothing to reconcile against
        open_trades.append(rec)
    open_trades.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return open_trades
