"""Home page aggregations.

Reads signals.jsonl (LLM proposals + outcomes) and trades.db (actual NT8 fills)
to build a single response the home page renders without extra round-trips.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter

from . import _tradebot_bridge as bridge  # noqa: F401  -- side-effect: sys.path
from . import db, settings as settings_mod, trades as tradelib
from .signals import _load_visible_signals
from src import instruments  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/home", tags=["home"])

logger = logging.getLogger(__name__)


def _account_category(account_name: str) -> str | None:
    """Return the bucket (live/evals/simulation) for an NT account, or None
    if uncategorized. Buckets are user-managed via the Settings page; this
    just reads from the live settings doc on each call (it's cached)."""
    accts = settings_mod.get_settings().accounts
    if account_name in accts.live:        return "live"
    if account_name in accts.evals:       return "evals"
    if account_name in accts.simulation:  return "simulation"
    return None


def _today_local_iso() -> str:
    return date.today().isoformat()


@router.get("")
def home() -> dict[str, Any]:
    """Single endpoint serving everything the home page needs."""
    visible = _load_visible_signals()
    config = instruments.load_config()
    today = _today_local_iso()

    # Enrich signals with metrics so realized P/L is computable.
    enriched: list[dict] = []
    for sig in visible.values():
        m = instruments.compute_trade_metrics(sig, config)
        enriched.append({**sig, "metrics": m})
    enriched.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    # ---- Today's snapshot ----
    today_signals = [s for s in enriched if s.get("timestamp", "")[:10] == today]
    today_with_pnl = [s for s in today_signals
                      if s["metrics"].get("realized_pnl") is not None]
    today_pnl = sum(s["metrics"]["realized_pnl"] for s in today_with_pnl)
    today_wins = sum(1 for s in today_with_pnl if s["metrics"]["realized_pnl"] > 0)
    today_losses = sum(1 for s in today_with_pnl if s["metrics"]["realized_pnl"] < 0)
    today_instruments = sorted({
        s.get("proposal", {}).get("instrument", "?") for s in today_signals
    })

    # Today's trades from the recorder (independent source of truth).
    today_trades_count = 0
    today_trades_pnl = 0.0
    try:
        fills_rows = db.fetch_fills_for_derivation(date_from=today + "T00:00:00")
        trades_rows = tradelib.derive_trades(fills_rows)
        today_trades_count = len(trades_rows)
        today_trades_pnl = sum(t["net_pnl"] for t in trades_rows)
    except FileNotFoundError:
        pass

    # ---- Action queue ----
    below_floor: list[dict] = []
    missing_journal: list[dict] = []
    for s in enriched:
        proposal = s.get("proposal") or {}
        outcome = s.get("outcome") or {}
        floor = proposal.get("confidence_floor") or 0.75
        conf = proposal.get("confidence")
        if conf is not None and conf < floor and not outcome.get("result"):
            below_floor.append({
                "timestamp": s["timestamp"],
                "instrument": proposal.get("instrument"),
                "confidence": conf,
                "floor": floor,
            })
        verdict = (s.get("journal") or {}).get("verdict")
        if not verdict:
            missing_journal.append({
                "timestamp": s["timestamp"],
                "instrument": proposal.get("instrument"),
            })

    # ---- Cumulative earnings by account category ----
    # All-time totals across four buckets:
    #   live        - real brokerage account(s)
    #   evals       - prop firm eval accounts (Tradify, Topstep, etc.)
    #   simulation  - sim/demo/playback/backtest accounts
    #   signals     - realized P/L from the LLM-proposed trades in signals.jsonl
    # Bucket membership is user-configured via the Settings page.
    cumulative_earnings = {"live": 0.0, "evals": 0.0, "simulation": 0.0, "signals": 0.0}
    try:
        all_fills = db.fetch_fills_for_derivation()
        all_trades = tradelib.derive_trades(all_fills)
        for t in all_trades:
            cat = _account_category(t.get("account", ""))
            if cat is not None:
                cumulative_earnings[cat] += t.get("net_pnl", 0.0) or 0.0
    except FileNotFoundError:
        pass
    for s in enriched:
        pnl = s["metrics"].get("realized_pnl")
        if pnl is not None:
            cumulative_earnings["signals"] += pnl
    cumulative_earnings = {k: round(v, 2) for k, v in cumulative_earnings.items()}

    return {
        "today": {
            "date": today,
            "signal_count": len(today_signals),
            "realized_pnl": round(today_pnl, 2),
            "win_count": today_wins,
            "loss_count": today_losses,
            "instruments": today_instruments,
            "trade_count": today_trades_count,
            "trade_pnl": round(today_trades_pnl, 2),
        },
        "action_queue": {
            "below_floor": below_floor[:10],
            "missing_journal": missing_journal[:10],
            "total": len(below_floor) + len(missing_journal),
        },
        "open_positions": [],  # TODO: depends on NS account-state indicator
        "cumulative_earnings": cumulative_earnings,
        "last_signal": enriched[0] if enriched else None,
    }
