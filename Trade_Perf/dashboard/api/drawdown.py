"""Per-account drawdown tracker for prop-firm Eval / Funded accounts.

Reads realized P&L from trades.db via the existing fill -> trade derivation
pipeline; layers per-account drawdown limits from Settings on top.

For each configured account, computes:
  - current_balance    = starting_balance + cumulative net P&L
  - peak_balance       = running max of historical balances
  - trailing_dd_used   = peak_balance - current_balance
  - trailing_dd_left   = trailing_drawdown - trailing_dd_used  (None if N/A)
  - today_pnl          = sum of trades whose exit_time falls in the local
                         calendar day per the Settings timezone
  - daily_dd_used      = max(0, -today_pnl)
  - daily_dd_left      = daily_drawdown - daily_dd_used
  - profit_target_left = profit_target - (current_balance - starting_balance)
  - status             = ok | warn | breach
                         warn  = either DD buffer < 25% of limit
                         breach = either DD buffer <= 0 (account would be busted)

Unrealized P&L from open positions is NOT included in v1 -- we mirror
trades.db (closed round-trips). A future iteration could read NT8's live
SQLite for open-position MTM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from . import db, settings as settings_mod, trades as tradelib
from .trading_day import current_trading_day, trading_day_bounds_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/drawdown", tags=["drawdown"])

WARN_THRESHOLD = 0.25     # alert when remaining buffer < 25% of limit


def _classify(buffer_left: float | None, limit: float | None) -> str:
    """Map remaining-buffer + limit to {ok, warn, breach}."""
    if buffer_left is None or limit is None or limit <= 0:
        return "ok"
    if buffer_left <= 0:
        return "breach"
    if (buffer_left / limit) < WARN_THRESHOLD:
        return "warn"
    return "ok"


def _account_drawdown(
    account_id: str,
    cfg: settings_mod.DrawdownConfig,
    trades_for_account: list[dict],
    today_start_utc: datetime,
    today_end_utc: datetime,
) -> dict[str, Any]:
    """Compute drawdown state for one account given its trade history."""
    # Walk closed trades in chronological order, tracking peak balance.
    chrono = sorted(trades_for_account, key=lambda t: t["exit_time"])
    balance = cfg.starting_balance
    peak = balance
    for t in chrono:
        balance += t["net_pnl"]
        if balance > peak:
            peak = balance
    current_balance = balance

    realized_pnl_total = current_balance - cfg.starting_balance
    trailing_used = max(0.0, peak - current_balance)
    trailing_left = cfg.trailing_drawdown - trailing_used

    # Today's P&L: trades whose exit_time falls in today's local window.
    today_pnl = 0.0
    for t in chrono:
        try:
            ts = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if today_start_utc <= ts < today_end_utc:
            today_pnl += t["net_pnl"]
    daily_used = max(0.0, -today_pnl)
    daily_left = cfg.daily_drawdown - daily_used

    profit_target_left = cfg.profit_target - realized_pnl_total

    # Status uses the WORSE of the two buffers.
    trailing_status = _classify(trailing_left, cfg.trailing_drawdown)
    daily_status    = _classify(daily_left,    cfg.daily_drawdown)
    priority = {"ok": 0, "warn": 1, "breach": 2}
    status = trailing_status if priority[trailing_status] >= priority[daily_status] else daily_status

    return {
        "account":             account_id,
        "starting_balance":    cfg.starting_balance,
        "current_balance":     round(current_balance, 2),
        "peak_balance":        round(peak, 2),
        "realized_pnl_total":  round(realized_pnl_total, 2),
        "today_pnl":           round(today_pnl, 2),
        "trailing_drawdown":   cfg.trailing_drawdown,
        "trailing_dd_used":    round(trailing_used, 2),
        "trailing_dd_left":    round(trailing_left, 2),
        "daily_drawdown":      cfg.daily_drawdown,
        "daily_dd_used":       round(daily_used, 2),
        "daily_dd_left":       round(daily_left, 2),
        "profit_target":       cfg.profit_target,
        "profit_target_left":  round(profit_target_left, 2),
        "profit_target_hit":   realized_pnl_total >= cfg.profit_target,
        "trade_count":         len(chrono),
        "trailing_status":     trailing_status,
        "daily_status":        daily_status,
        "status":              status,
    }


@router.get("/accounts")
def list_drawdowns() -> dict[str, Any]:
    """Return drawdown state for every account that has a config in Settings.

    Accounts without a drawdown config aren't included -- the feature is opt-in
    per account (configured on the Accounts tab of the Settings page).
    """
    cfg = settings_mod.get_settings().accounts
    visible = settings_mod.visible_accounts()
    configs = {a: c for a, c in (cfg.drawdowns or {}).items() if a in visible}
    if not configs:
        return {"accounts": [], "warn_threshold": WARN_THRESHOLD, "tz": settings_mod.get_settings().appearance.timezone}

    tz_name = settings_mod.get_settings().appearance.timezone
    # Daily DD window = current trading day (CME-style 5 PM CT roll), not the
    # calendar day. Trades closed after the local 6 PM roll bucket into the
    # NEW trading day's daily-DD allowance.
    today = current_trading_day(tz_name)
    today_start, today_end = trading_day_bounds_utc(today, tz_name)

    # Pull every fill for the configured accounts, derive round-trips, group.
    account_ids = list(configs.keys())
    try:
        fills = db.fetch_fills_for_derivation(account=account_ids)
    except FileNotFoundError:
        return {"accounts": [], "warn_threshold": WARN_THRESHOLD, "tz": tz_name,
                "error": "trades.db not found"}
    trades_all = tradelib.derive_trades(fills)

    by_account: dict[str, list[dict]] = {a: [] for a in account_ids}
    for t in trades_all:
        if t["account"] in by_account:
            by_account[t["account"]].append(t)

    rows = []
    for acct, dd_cfg in configs.items():
        row = _account_drawdown(acct, dd_cfg, by_account.get(acct, []), today_start, today_end)
        rows.append(row)

    # Sort by status severity then by account name so worst-off bubbles up.
    priority = {"breach": 0, "warn": 1, "ok": 2}
    rows.sort(key=lambda r: (priority[r["status"]], r["account"]))

    return {
        "accounts":       rows,
        "warn_threshold": WARN_THRESHOLD,
        "tz":             tz_name,
    }
