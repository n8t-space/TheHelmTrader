"""Signal <-> NT8 fill integrity auditor.

Trade Performance derives P&L from real broker fills (trades.db). Signal Analysis
shows the tick-resolver's PAPER outcome. They diverge when the idealized bracket
walk disagrees with what the ATM actually filled -- the resolver can book a TP1
"win" on a trade the account actually stopped out for a loss.

This auditor closes that gap. It links each executed signal to its real round-trip
(via fill_linker), and where the paper P&L disagrees with the broker net it stamps
the signal with the REAL net P&L (``audit.source == "fills"``). instruments.
compute_trade_metrics then surfaces that as the signal's realized P&L, so Signal
Analysis and Trade Performance agree.

Ground rules (operator's words): "trades data matches the NT database, no guessing
or adjusting allowed." So:
  * NT fills are the only source of truth. We never invent a number.
  * A filled signal we cannot confidently link is flagged ``unverified`` -- left as
    paper, never silently "corrected" to a guess.
  * Every correction is appended to an immutable audit log.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from . import _tradebot_bridge as bridge
from . import fill_linker
from . import settings as settings_mod

from src import instruments  # noqa: E402  (via bridge sys.path)
from src import signal_storage  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auditor", tags=["auditor"])

# A correction fires only when broker net and paper realized differ by more than
# this (dollars). Below it they already agree -- nothing to fix.
RECONCILE_EPS = 0.01

AUDIT_LOG = bridge.SIGNALS_LOG.parent / "audit_log.jsonl"

# Long enough that the sweep never fires inside a test session's short-lived
# TestClient lifespans (which would hammer the live trades.db mid-test); in
# production it just delays the first full sweep ~10 min after a cold start.
FIRST_RUN_DELAY_S = 600

# Responsive pass: a multi-bracket signal's paper resolver freezes the runner
# 'neither' the moment feed.db tape runs out, so it shows "first win only" until
# the hourly sweep corrects it. This fast loop reconciles just the RECENTLY
# filled signals against real fills every RECENT_INTERVAL_S, so an executed
# scale-out matches Trade Performance within ~a minute of the real trade closing
# (a still-open runner has no closed round-trip to link, so it's left alone).
RECENT_FILL_WINDOW_S = 12 * 3600
RECENT_FIRST_DELAY_S = 60
RECENT_INTERVAL_S    = 90

# In-process status, surfaced by GET /status. Reset on restart; the durable
# record is the audit log + each signal's own ``audit`` block.
_state: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_summary": None,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _paper_realized(rec: dict, config: dict) -> tuple[float | None, str | None]:
    """The signal's realized P&L IGNORING any prior auditor override -- i.e. what
    the paper resolver alone concluded. Used as the 'before' value for logging and
    to decide whether a correction is even needed."""
    probe = dict(rec)
    probe.pop("audit", None)
    m = instruments.compute_trade_metrics(probe, config)
    return m.get("realized_pnl"), m.get("realized_pnl_source")


def _aggregate_result(legs: list[dict]) -> str:
    """Collapse real-fill legs to one outcome label (mirrors the watcher's rule):
    all winners -> 'target', all losers -> 'stop', anything mixed -> 'partial'.
    A linked trade is a CLOSED round-trip, so there is always a verdict."""
    results = [leg.get("result") for leg in legs]
    if results and all(r == "target" for r in results):
        return "target"
    if results and all(r == "stop" for r in results):
        return "stop"
    return "partial"


def _real_legs(trade: dict) -> list[dict]:
    """Build leg records from a trade's real exit fills so the detail view shows
    actual TP/SL/trail fills instead of the resolver's imagined ones."""
    legs: list[dict] = []
    for i, f in enumerate(trade.get("exit_fills") or []):
        pnl = f.get("pnl")
        if pnl is None:
            result = "be"
        else:
            result = "target" if pnl > 0 else "stop" if pnl < 0 else "be"
        legs.append({
            "bracket_idx": i,
            "qty": f.get("qty"),
            "exit_price": f.get("price"),
            "exit_ts": f.get("time"),
            "result": result,
            "pnl": pnl,
            "method": "fill",
            "engine": "auditor",
            "open": False,
        })
    return legs


def _append_log(entry: dict) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_log(limit: int = 200) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    rows: list[dict] = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:][::-1]  # newest first


def reconcile(signals: dict[str, dict], links: dict[str, dict], config: dict) -> dict:
    """Pure reconciliation core (no I/O of its own beyond writing corrections).

    Returns a summary dict. Each filled directional signal is one of:
      corrected  -- linked, broker net disagreed with paper -> stamped to fills
      in_sync    -- linked and already agrees (paper==broker, or prior correction)
      unverified -- filled but no confident fill link -> flagged, left as paper
    """
    checked = corrected = in_sync = unverified = 0
    details: list[dict] = []
    checked_at = _now_iso()

    for ts, rec in signals.items():
        if rec.get("deleted"):
            continue
        proposal = rec.get("proposal") or {}
        direction = (proposal.get("direction") or "").lower()
        ex = rec.get("exec") or {}
        # Only signals that actually placed a trade are auditable against fills.
        if direction not in ("long", "short") or ex.get("state") != "filled":
            continue
        checked += 1

        link = links.get(ts)
        instrument = proposal.get("instrument")
        account = ex.get("account") or rec.get("arm_account")

        if not link:
            # Cannot tie it to a real trade -> we refuse to guess. Flag it.
            prior = rec.get("audit") or {}
            if prior.get("source") != "unlinked" or not prior.get("checked_at"):
                signal_storage.append_update(
                    bridge.SIGNALS_LOG, ts,
                    audit={"source": "unlinked", "needs_review": True,
                           "checked_at": checked_at},
                )
            unverified += 1
            details.append({"signal_ts": ts, "instrument": instrument,
                            "account": account, "action": "unverified"})
            continue

        trade = link["trade"]
        confidence = link.get("confidence")
        real_net = round(float(trade.get("net_pnl") or 0.0), 2)
        real_qty = trade.get("qty")
        paper, paper_src = _paper_realized(rec, config)

        prior = rec.get("audit") or {}
        already = (prior.get("source") == "fills"
                   and prior.get("realized_pnl") is not None
                   and abs(float(prior["realized_pnl"]) - real_net) <= RECONCILE_EPS)

        agrees = paper is not None and abs(float(paper) - real_net) <= RECONCILE_EPS

        outcome_closed = (rec.get("outcome") or {}).get("result") not in (None, "", "pending")

        if (already or agrees) and outcome_closed:
            in_sync += 1
            continue

        if already or agrees:
            # P&L already matches fills, but the CLOSED outcome was never stamped
            # (the paper resolver left a runner open, so Signal Analysis still
            # shows the trade pending). Backfill the real legs + aggregate outcome
            # -- no P&L change, so this isn't a "correction".
            real_legs = _real_legs(trade)
            signal_storage.append_update(
                bridge.SIGNALS_LOG, ts,
                legs=real_legs,
                outcome={
                    "result": _aggregate_result(real_legs),
                    "note": "Auditor: resolved from real NT8 fills",
                    "closing_price": None,
                    "auto_confirmed": True,
                    "auditor_resolved": True,
                },
                entry_triggered=True,
            )
            in_sync += 1
            details.append({"signal_ts": ts, "instrument": instrument,
                            "account": account, "action": "outcome_backfill"})
            continue

        # Mismatch, with a confident link -> book the broker truth.
        audit_block = {
            "source": "fills",
            "realized_pnl": real_net,
            "real_gross_pnl": round(float(trade.get("gross_pnl") or 0.0), 2),
            "real_qty": real_qty,
            "real_entry": trade.get("entry_price"),
            "real_direction": trade.get("direction"),
            "real_exits": trade.get("exit_fills"),
            "confidence": confidence,
            "trade_key": f"{trade.get('account')}|{trade.get('exit_time')}",
            "prev_realized": None if paper is None else round(float(paper), 2),
            "prev_source": paper_src,
            "checked_at": checked_at,
        }
        real_legs = _real_legs(trade)
        # The linked trade is a closed round-trip, so stamp the aggregate outcome
        # too -- otherwise Signal Analysis keeps showing the trade "open" even
        # though its legs and P&L are resolved from real fills.
        agg = _aggregate_result(real_legs)
        signal_storage.append_update(
            bridge.SIGNALS_LOG, ts,
            audit=audit_block,
            legs=real_legs,
            outcome={
                "result": agg,
                "note": "Auditor: resolved from real NT8 fills",
                "closing_price": None,
                "auto_confirmed": True,
                "auditor_resolved": True,
            },
            entry_triggered=True,
        )
        corrected += 1
        log_entry = {
            "checked_at": checked_at,
            "signal_ts": ts,
            "instrument": instrument,
            "account": account,
            "action": "corrected",
            "prev_realized": audit_block["prev_realized"],
            "prev_source": paper_src,
            "new_realized": real_net,
            "confidence": confidence,
        }
        _append_log(log_entry)
        details.append(log_entry)
        logger.warning(
            "[auditor] corrected %s %s: paper=%s (%s) -> fills=$%.2f (conf %s)",
            ts, instrument, audit_block["prev_realized"], paper_src, real_net,
            confidence,
        )

    return {
        "checked_at": checked_at,
        "checked": checked,
        "corrected": corrected,
        "in_sync": in_sync,
        "unverified": unverified,
        "details": details,
    }


def _filled_within(rec: dict, window_s: float) -> bool:
    """True if the signal was auto-filled within the last ``window_s`` seconds.
    Used to scope the responsive pass to fresh trades."""
    ex = rec.get("exec") or {}
    if ex.get("state") != "filled":
        return False
    try:
        dt = datetime.fromisoformat(ex.get("filled_at"))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (datetime.now() - dt).total_seconds() <= window_s


def run_audit(*, recent_only: bool = False) -> dict:
    """Load live data, reconcile, persist. Blocking -- call via to_thread.

    ``recent_only`` scopes reconciliation to signals filled within
    RECENT_FILL_WINDOW_S (the responsive pass); the full sweep leaves it False.
    """
    _state["running"] = True
    try:
        sigs = signal_storage.load_all(bridge.SIGNALS_LOG)
        config = instruments.load_config()
        links = fill_linker.build_links()
        if recent_only:
            sigs = {ts: r for ts, r in sigs.items()
                    if _filled_within(r, RECENT_FILL_WINDOW_S)}
        summary = reconcile(sigs, links, config)
        # The responsive pass is a partial view; don't let it overwrite the
        # full-sweep status counters the UI shows.
        if not recent_only:
            _state["last_run"] = summary["checked_at"]
            _state["last_summary"] = {k: summary[k] for k in
                                      ("checked", "corrected", "in_sync", "unverified")}
        return summary
    except Exception:
        logger.exception("[auditor] run failed")
        raise
    finally:
        _state["running"] = False


async def audit_recent_loop_forever() -> None:
    """Responsive pass: reconcile freshly filled signals against real fills so an
    executed multi-bracket trade matches Trade Performance within ~a minute."""
    await asyncio.sleep(RECENT_FIRST_DELAY_S)
    while True:
        if settings_mod.get_settings().auditor.enabled:
            try:
                summary = await asyncio.to_thread(run_audit, recent_only=True)
                if summary["corrected"]:
                    logger.info("[auditor] responsive pass corrected %d fresh "
                                "signal(s)", summary["corrected"])
            except Exception:
                logger.exception("[auditor] responsive pass failed")
        await asyncio.sleep(RECENT_INTERVAL_S)


async def audit_loop_forever() -> None:
    """Background hourly (configurable) integrity sweep."""
    await asyncio.sleep(FIRST_RUN_DELAY_S)
    while True:
        cfg = settings_mod.get_settings().auditor
        interval = max(5, cfg.interval_minutes) * 60
        if cfg.enabled:
            try:
                summary = await asyncio.to_thread(run_audit)
                logger.info("[auditor] swept: %d checked, %d corrected, "
                            "%d unverified", summary["checked"],
                            summary["corrected"], summary["unverified"])
            except Exception:
                logger.exception("[auditor] loop iteration failed")
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- API


@router.get("/status")
async def get_status() -> dict:
    cfg = settings_mod.get_settings().auditor
    return {
        "enabled": cfg.enabled,
        "interval_minutes": cfg.interval_minutes,
        "running": _state["running"],
        "last_run": _state["last_run"],
        "last_summary": _state["last_summary"],
        "recent": read_log(limit=50),
    }


@router.post("/run")
async def run_now() -> dict:
    summary = await asyncio.to_thread(run_audit)
    return {
        "checked": summary["checked"],
        "corrected": summary["corrected"],
        "in_sync": summary["in_sync"],
        "unverified": summary["unverified"],
        "checked_at": summary["checked_at"],
        "details": summary["details"],
    }


@router.get("/log")
async def get_log(limit: int = 200) -> dict:
    return {"entries": read_log(limit=limit)}
