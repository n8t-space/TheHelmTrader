"""Signal Analysis routes.

Reads/writes against TradingBot's signals.jsonl via the _tradebot_bridge
sys.path shim. Mutating routes (journal, outcome, position, suggestion,
reject, delete) are still stubs through Checkpoint 3 — they're wired in
Checkpoint 4 alongside the detail page.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge
from src import instruments, signal_storage  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/signals", tags=["signals"])

logger = logging.getLogger(__name__)


def _load_visible_signals() -> dict[str, dict]:
    """Loads + merges signals.jsonl (latest-wins), filters out soft-deleted
    entries. The cross-signal LLM reconciliation feature was removed
    2026-05-19; outcome_suggestion still exists on records (audit trail
    from the outcome_resolver bar-walker) but no UI surfaces it."""
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    return {ts: rec for ts, rec in raw.items() if not rec.get("deleted")}


OUTCOME_RESULTS = ("pending", "target", "stop", "breakeven", "partial", "no_fill", "not_watched", "other")


class JournalUpdate(BaseModel):
    # The legacy agree/disagree/skip verdict was dropped 2026-05-13 -- the
    # journal is now a free-form note field. Old records that carried a
    # verdict still load (signal_storage stores dicts, not Pydantic), but
    # the UI no longer shows or sets it.
    note: str | None = None


class OutcomeUpdate(BaseModel):
    result: str = Field(..., pattern=f"^({'|'.join(OUTCOME_RESULTS)})$")
    note: str | None = None
    closing_price: float | None = None


class PositionUpdate(BaseModel):
    position_size: float = Field(..., ge=0)


class EntryTriggeredUpdate(BaseModel):
    triggered: bool


# Per-leg fill record. Frontend POSTs the full legs array on each save;
# server writes it verbatim so the latest-wins merge picks up the new value.
LEG_RESULTS = ("target", "stop", "trail", "be", "manual", "neither")


class LegFill(BaseModel):
    bracket_idx: int = Field(..., ge=0)
    qty: float = Field(..., gt=0)
    result: str = Field(..., pattern=f"^({'|'.join(LEG_RESULTS)})$")
    exit_price: float | None = None
    exit_ts: int | None = None       # unix milliseconds
    method: str | None = None        # 'tick' | 'bar' | 'manual'
    engine: str | None = None        # 'resolver' | 'manual'


class LegsUpdate(BaseModel):
    legs: list[LegFill]


def _enrich(rec: dict, config: dict) -> dict:
    """Attach computed trade metrics + screenshot filename to a signal record."""
    rec = dict(rec)
    rec["metrics"] = instruments.compute_trade_metrics(rec, config)
    sp = rec.get("screenshot_path")
    rec["screenshot_filename"] = Path(sp).name if sp else None
    return rec


@router.get("")
def list_signals(include_deleted: bool = False) -> dict[str, Any]:
    """Return all signals, latest-wins merged from signals.jsonl, newest first."""
    if include_deleted:
        signals = signal_storage.load_all(bridge.SIGNALS_LOG)
    else:
        signals = _load_visible_signals()
    config = instruments.load_config()
    rows = [_enrich(r, config) for r in
            sorted(signals.values(), key=lambda r: r.get("timestamp", ""), reverse=True)]
    return {"count": len(rows), "signals": rows}


@router.get("/{timestamp}")
def get_signal(timestamp: str) -> dict[str, Any]:
    """Return one signal enriched with computed metrics."""
    signals = _load_visible_signals()
    if timestamp not in signals:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"signal not found: {timestamp}")
    config = instruments.load_config()
    return {"signal": _enrich(signals[timestamp], config)}


def _require_signal(timestamp: str, *, include_deleted: bool = False) -> dict:
    if include_deleted:
        signals = signal_storage.load_all(bridge.SIGNALS_LOG)
    else:
        signals = _load_visible_signals()
    rec = signals.get(timestamp)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"signal not found: {timestamp}")
    return rec


@router.post("/capture", status_code=status.HTTP_201_CREATED)
def capture() -> dict[str, Any]:
    """Open the Windows Snipping overlay, run the pipeline, return the new signal.

    Synchronous: blocks until the user has snipped and the LLM has produced a
    proposal (typically 5-30 s on the workstation; 30-60 s on first cold call).
    The frontend should expect a long request and surface a clear pending state.
    """
    from src.pipeline import run_pipeline  # type: ignore[import-not-found]

    try:
        prompt = bridge.PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not read analyzer prompt: {e}") from e

    try:
        record = run_pipeline(bridge.SCREENSHOTS_DIR, bridge.SIGNALS_LOG, prompt)
    except RuntimeError as e:
        # Common case: user closed the snip overlay without capturing.
        logger.warning("Capture rejected: %s", e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Pipeline failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"pipeline error: {e}") from e

    config = instruments.load_config()
    return {"signal": _enrich(record, config)}


@router.post("/{timestamp}/journal")
def update_journal(timestamp: str, update: JournalUpdate) -> dict[str, Any]:
    _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        journal={"note": update.note},
    )
    return {"timestamp": timestamp, "journal": update.model_dump()}


@router.post("/{timestamp}/outcome")
def update_outcome(timestamp: str, update: OutcomeUpdate) -> dict[str, Any]:
    """Set the outcome and coerce entry_triggered to match the rule:
    outcome populated implies the entry was hit, so any non-no_fill
    outcome forces entry_triggered=True; outcome=no_fill forces False.
    Reject the contradictory case (already flagged not-entered, asking
    for a real outcome) so stats stay honest."""
    rec = _require_signal(timestamp)
    if rec.get("entry_triggered") is False and update.result != "no_fill":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Signal was not entered (entry_triggered=false); outcome is locked to 'no_fill'.",
        )
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        outcome={"result": update.result, "note": update.note,
                 "closing_price": update.closing_price},
        entry_triggered=(update.result != "no_fill"),
    )
    return {"timestamp": timestamp, "outcome": update.model_dump(),
            "entry_triggered": update.result != "no_fill"}


@router.post("/{timestamp}/position")
def update_position(timestamp: str, update: PositionUpdate) -> dict[str, Any]:
    _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        position_size=update.position_size,
    )
    return {"timestamp": timestamp, "position_size": update.position_size}


@router.post("/{timestamp}/legs")
def update_legs(timestamp: str, update: LegsUpdate) -> dict[str, Any]:
    """Write per-leg fills for a multi-bracket scale-out trade.

    User-entered legs overwrite any auto-resolved legs from outcome_watcher;
    each entry gets engine='manual' unless the caller stamped it otherwise.
    The aggregate outcome.result is left alone -- the user (or the watcher)
    sets that separately via /outcome.
    """
    _require_signal(timestamp)
    legs_out: list[dict] = []
    for leg in update.legs:
        d = leg.model_dump(exclude_none=False)
        if not d.get("engine"):
            d["engine"] = "manual"
        legs_out.append(d)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp, legs=legs_out,
    )
    return {"timestamp": timestamp, "legs": legs_out}


@router.post("/{timestamp}/entry-triggered")
def update_entry_triggered(timestamp: str, update: EntryTriggeredUpdate) -> dict[str, Any]:
    """Mark whether the proposal's entry price was actually hit + the user
    took the trade. Distinct from outcome (how it closed) and position_size
    (sizing decision). Toggled from the Signal Analysis row's Entry-column
    checkbox; surfaces in trade-history filters.

    Flipping triggered=False stamps outcome=no_fill when no outcome is set
    yet so the signal flows into the W/L + P&L rollups without a separate
    edit. An existing outcome (target/stop/etc.) is left alone."""
    rec = _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        entry_triggered=update.triggered,
    )
    auto_no_fill = False
    if not update.triggered and not (rec.get("outcome") or {}).get("result"):
        signal_storage.append_update(
            bridge.SIGNALS_LOG, timestamp,
            outcome={"result": "no_fill",
                     "note": "Auto: marked no-entry on the dashboard",
                     "closing_price": None,
                     "auto_confirmed": True},
        )
        auto_no_fill = True
    return {"timestamp": timestamp,
            "entry_triggered": update.triggered,
            "auto_no_fill_outcome": auto_no_fill}


@router.post("/{timestamp}/reject")
def reject_analysis(timestamp: str) -> dict[str, Any]:
    """Soft-delete THIS signal entirely — used when the model's reconciliation
    read is wrong and you don't want any of its suggestions applied. Prior open
    trades are left untouched.
    """
    _require_signal(timestamp)
    signal_storage.append_update(bridge.SIGNALS_LOG, timestamp, deleted=True)
    logger.info("Rejected analysis %s — soft-deleted", timestamp)
    return {"timestamp": timestamp, "deleted": True}


@router.delete("/{timestamp}", status_code=status.HTTP_204_NO_CONTENT)
def delete_signal(timestamp: str) -> None:
    """Soft-delete: append a deleted=true update. Filtered out of dashboard views."""
    _require_signal(timestamp, include_deleted=True)
    signal_storage.append_update(bridge.SIGNALS_LOG, timestamp, deleted=True)
    logger.info("Soft-deleted signal %s", timestamp)
    return None
