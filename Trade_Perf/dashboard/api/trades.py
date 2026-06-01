"""Round-trip trade derivation and aggregate statistics from the fills table.

A 'trade' is the sequence of fills within (account, master_symbol) starting
when position leaves 0 and ending when it returns to 0. NT8 already gives us
per-symbol running 'position' on each fill, so we use that as the boundary
signal -- no need to re-walk position state ourselves.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .trading_day import trading_day_for_ts


# Action codes that increase position (long-side opens / short-covers)
LONG_OPEN_ACTIONS = {"Buy", "BuyToCover"}


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _build_trade(group: list[dict]) -> dict | None:
    if not group:
        return None
    entries = [f for f in group if f["is_entry"] == 1]
    exits = [f for f in group if f["is_exit"] == 1]
    if not entries or not exits:
        return None

    direction = "Long" if entries[0]["order_action"] in LONG_OPEN_ACTIONS else "Short"

    entry_qty = sum(f["qty"] for f in entries)
    exit_qty = sum(f["qty"] for f in exits)
    qty = max(entry_qty, exit_qty) or 0

    avg_entry = (sum(f["price"] * f["qty"] for f in entries) / entry_qty) if entry_qty else 0.0
    avg_exit = (sum(f["price"] * f["qty"] for f in exits) / exit_qty) if exit_qty else 0.0

    pv = entries[0].get("point_value") or 1.0

    if direction == "Long":
        gross = (avg_exit - avg_entry) * qty * pv
    else:
        gross = (avg_entry - avg_exit) * qty * pv

    commission = sum((f["commission"] or 0.0) for f in group)
    fee = sum((f["fee"] or 0.0) for f in group)
    net = gross - commission - fee

    # Prefer the ATM template name (e.g. '40 for 400') over the generic
    # 'AtmStrategy' class label NT writes for all ATM-driven fills.
    strategies = sorted({
        (f.get("strategy_template") or f.get("strategy_name"))
        for f in group
        if f.get("strategy_template") or f.get("strategy_name")
    })

    entry_time = entries[0]["time_utc"]
    exit_time = exits[-1]["time_utc"]
    duration = (_parse_iso(exit_time) - _parse_iso(entry_time)).total_seconds()

    # Per-fill detail so the dashboard can show TP1 vs Runner fills on scale-out
    # ATMs instead of a misleading volume-weighted average. Each fill carries
    # its own dollar P&L (vs entry's avg) so the UI doesn't have to recompute.
    sign = 1 if direction == "Long" else -1

    def _fill_summary(rows: list[dict]) -> list[dict]:
        return [
            {
                "time":  f["time_utc"],
                "qty":   f["qty"],
                "price": f["price"],
            }
            for f in rows
        ]

    entry_fills = _fill_summary(entries)
    exit_fills_detailed = []
    for f in exits:
        leg_pnl = sign * (f["price"] - avg_entry) * f["qty"] * pv
        exit_fills_detailed.append({
            "time":  f["time_utc"],
            "qty":   f["qty"],
            "price": f["price"],
            "pnl":   round(leg_pnl, 2),
        })

    # Scale-out heuristic: NT8 records each bracket's TP/SL/trail fill as a
    # separate execution. >1 exit row means the trade closed in legs.
    is_scale_out = len(exits) > 1

    return {
        "account": group[0]["account_name"],
        "symbol": group[0]["master_symbol"],
        "contract": group[0]["symbol"],
        "direction": direction,
        "qty": qty,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": round(avg_entry, 4),
        "exit_price": round(avg_exit, 4),
        "point_value": pv,
        "gross_pnl": round(gross, 2),
        "commission": round(commission, 2),
        "fee": round(fee, 2),
        "net_pnl": round(net, 2),
        "duration_seconds": round(duration, 1),
        "strategies": strategies,
        "num_fills": len(group),
        "first_fill_id": group[0]["id"],
        "last_fill_id": group[-1]["id"],
        "is_scale_out": is_scale_out,
        "entry_fills": entry_fills,
        "exit_fills": exit_fills_detailed,
    }


def derive_trades(fills: list[dict]) -> list[dict]:
    """Group fills by (account, master_symbol) and partition into round-trips.

    Boundary signal: the running 'position' column from NT8 returning to 0.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in fills:
        if not f.get("master_symbol") or not f.get("account_name"):
            continue
        groups[(f["account_name"], f["master_symbol"])].append(f)

    trades: list[dict] = []
    for group in groups.values():
        group.sort(key=lambda f: (f["time_utc"], f["id"]))
        active: list[dict] = []
        for f in group:
            active.append(f)
            if f["position"] == 0:
                trade = _build_trade(active)
                if trade is not None:
                    trades.append(trade)
                active = []
        # Open positions (active still non-empty) intentionally dropped for v1.

    trades.sort(key=lambda t: t["exit_time"], reverse=True)
    return trades


def compute_stats(trades: list[dict], *, tz: str | None = None) -> dict:
    """Aggregate stats for a set of trades, plus equity curve and breakdowns.

    Keys ``daily_pnl`` by **trading day** (CME-style 5 PM CT roll) instead of
    raw UTC date, so trades that close after the operator's local 5 PM but
    before UTC midnight don't end up booked to the wrong calendar day.
    """
    if not trades:
        return _empty_stats()

    asc = sorted(trades, key=lambda t: t["exit_time"])
    pnls = [t["net_pnl"] for t in asc]
    gross_pnls = [t["gross_pnl"] for t in asc]
    commissions = [t["commission"] + t["fee"] for t in asc]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flats = [p for p in pnls if p == 0]

    cum = 0.0
    equity_curve: list[dict] = []
    peak = 0.0
    max_dd = 0.0
    for t, p in zip(asc, pnls):
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
        equity_curve.append({
            "exit_time": t["exit_time"],
            "cumulative_net_pnl": round(cum, 2),
            "drawdown": round(dd, 2),
        })

    by_day: dict[str, float] = defaultdict(float)
    for t, p in zip(asc, pnls):
        # Trading-day attribution: any trade closed at-or-after the operator's
        # local 6 PM rolls into the NEXT trading day. Falls back to UTC date
        # only if the timestamp can't be parsed (malformed legacy records).
        day = trading_day_for_ts(t["exit_time"], tz) or t["exit_time"][:10]
        by_day[day] += p
    daily = [{"date": d, "net_pnl": round(v, 2)} for d, v in sorted(by_day.items())]

    by_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0})
    for t in asc:
        s = t["symbol"] or "?"
        by_symbol[s]["trades"] += 1
        by_symbol[s]["net_pnl"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            by_symbol[s]["wins"] += 1
        elif t["net_pnl"] < 0:
            by_symbol[s]["losses"] += 1
    symbol_breakdown = [
        {"symbol": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}}
        for k, v in sorted(by_symbol.items(), key=lambda kv: -kv[1]["net_pnl"])
    ]

    by_strategy: dict[str, dict] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0})
    for t in asc:
        keys = t["strategies"] or ["(none)"]
        for k in keys:
            by_strategy[k]["trades"] += 1
            by_strategy[k]["net_pnl"] += t["net_pnl"]
            if t["net_pnl"] > 0:
                by_strategy[k]["wins"] += 1
            elif t["net_pnl"] < 0:
                by_strategy[k]["losses"] += 1
    strategy_breakdown = [
        {"strategy": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}}
        for k, v in sorted(by_strategy.items(), key=lambda kv: -kv[1]["net_pnl"])
    ]

    by_account: dict[str, dict] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0})
    for t in asc:
        a = t["account"] or "?"
        by_account[a]["trades"] += 1
        by_account[a]["net_pnl"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            by_account[a]["wins"] += 1
        elif t["net_pnl"] < 0:
            by_account[a]["losses"] += 1
    account_breakdown = [
        {"account": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}}
        for k, v in sorted(by_account.items(), key=lambda kv: -kv[1]["net_pnl"])
    ]

    profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf") if wins else 0.0

    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0,
        "gross_pnl": round(sum(gross_pnls), 2),
        "commissions_and_fees": round(sum(commissions), 2),
        "net_pnl": round(sum(pnls), 2),
        "avg_win": round((sum(wins) / len(wins)), 2) if wins else 0,
        "avg_loss": round((sum(losses) / len(losses)), 2) if losses else 0,
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "profit_factor": (round(profit_factor, 2)
                          if profit_factor != float("inf") else None),
        "max_drawdown": round(max_dd, 2),
        "equity_curve": equity_curve,
        "daily_pnl": daily,
        "by_symbol": symbol_breakdown,
        "by_strategy": strategy_breakdown,
        "by_account": account_breakdown,
    }


def _empty_stats() -> dict:
    return {
        "trade_count": 0, "win_count": 0, "loss_count": 0, "flat_count": 0,
        "win_rate": 0, "gross_pnl": 0, "commissions_and_fees": 0, "net_pnl": 0,
        "avg_win": 0, "avg_loss": 0, "best_trade": 0, "worst_trade": 0,
        "profit_factor": None, "max_drawdown": 0,
        "equity_curve": [], "daily_pnl": [],
        "by_symbol": [], "by_strategy": [], "by_account": [],
    }


__all__: Iterable[str] = ("derive_trades", "compute_stats")
