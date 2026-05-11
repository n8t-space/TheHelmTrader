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
    """Mirror TradingBot dashboard.py's load_signals(include_deleted=False).

    Loads + merges signals.jsonl (latest-wins), filters out soft-deleted entries,
    then strips stale outcome_suggestion entries whose source analysis was
    deleted (so prior trades don't show 'pending' banners pointing nowhere).
    """
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    visible = {ts: rec for ts, rec in raw.items() if not rec.get("deleted")}
    for rec in visible.values():
        sug = rec.get("outcome_suggestion")
        if sug:
            source_ts = sug.get("source_signal_ts")
            if not source_ts or source_ts not in visible:
                rec.pop("outcome_suggestion", None)
    return visible


JOURNAL_VERDICTS = ("agree", "disagree", "skip")
OUTCOME_RESULTS = ("pending", "target", "stop", "breakeven", "partial", "no_fill", "not_watched", "other")


class JournalUpdate(BaseModel):
    verdict: str = Field(..., pattern=f"^({'|'.join(JOURNAL_VERDICTS)})$")
    note: str | None = None


class OutcomeUpdate(BaseModel):
    result: str = Field(..., pattern=f"^({'|'.join(OUTCOME_RESULTS)})$")
    note: str | None = None
    closing_price: float | None = None


class PositionUpdate(BaseModel):
    position_size: float = Field(..., ge=0)


class EntryTriggeredUpdate(BaseModel):
    triggered: bool


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
    """Return one signal plus its reconciliation children.

    Children are visible signals whose `outcome_suggestion.source_signal_ts`
    points back to this signal and that don't already have a final outcome.
    Both the signal and each child are enriched with computed metrics.
    """
    signals = _load_visible_signals()
    if timestamp not in signals:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"signal not found: {timestamp}")
    config = instruments.load_config()
    rec = _enrich(signals[timestamp], config)

    children = []
    for ts, child in signals.items():
        if ts == timestamp:
            continue
        sug = child.get("outcome_suggestion") or {}
        if sug.get("source_signal_ts") != timestamp:
            continue
        if (child.get("outcome") or {}).get("result"):
            continue
        children.append(_enrich(child, config))
    children.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    return {"signal": rec, "children": children}


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
        journal={"verdict": update.verdict, "note": update.note},
    )
    return {"timestamp": timestamp, "journal": update.model_dump()}


@router.post("/{timestamp}/outcome")
def update_outcome(timestamp: str, update: OutcomeUpdate) -> dict[str, Any]:
    _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        outcome={"result": update.result, "note": update.note,
                 "closing_price": update.closing_price},
    )
    return {"timestamp": timestamp, "outcome": update.model_dump()}


@router.post("/{timestamp}/position")
def update_position(timestamp: str, update: PositionUpdate) -> dict[str, Any]:
    _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        position_size=update.position_size,
    )
    return {"timestamp": timestamp, "position_size": update.position_size}


@router.post("/{timestamp}/entry-triggered")
def update_entry_triggered(timestamp: str, update: EntryTriggeredUpdate) -> dict[str, Any]:
    """Mark whether the proposal's entry price was actually hit + the user
    took the trade. Distinct from outcome (how it closed) and position_size
    (sizing decision). Toggled from the Signal Analysis row's Entry-column
    checkbox; surfaces in trade-history filters."""
    _require_signal(timestamp)
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        entry_triggered=update.triggered,
    )
    return {"timestamp": timestamp, "entry_triggered": update.triggered}


@router.post("/{timestamp}/suggestion/confirm")
def confirm_suggestion(timestamp: str) -> dict[str, Any]:
    """Apply the suggested outcome AND soft-delete the prior signal — it's now resolved.

    Mirrors TradingBot dashboard.py confirm_suggestion. Outcome data persists in
    the JSONL for later analysis but the row is hidden from the dashboard.
    """
    rec = _require_signal(timestamp)
    suggestion = rec.get("outcome_suggestion") or {}
    if not suggestion or rec.get("outcome_suggestion_dismissed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no pending suggestion")
    note = f"Confirmed from suggestion: {(suggestion.get('reasoning') or '')[:200]}"
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        outcome={"result": suggestion["result"], "note": note, "closing_price": None},
    )
    signal_storage.append_update(bridge.SIGNALS_LOG, timestamp, deleted=True)
    logger.info("Confirmed %s for %s + soft-deleted (prior signal resolved)",
                suggestion["result"], timestamp)
    return {"timestamp": timestamp, "applied": suggestion["result"],
            "source_signal_ts": suggestion.get("source_signal_ts")}


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
