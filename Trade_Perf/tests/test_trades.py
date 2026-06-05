"""Round-trip derivation from NT8 fills.

Locks the direction + P&L fixes:
  * an ATM short whose entry NT labels 'BuyToCover' is read Short from the signed
    position (not Long from the action), keeping the P&L sign right,
  * scale-out exits are itemized with per-leg P&L.
"""
from __future__ import annotations

import math

from dashboard.api.trades import derive_trades, compute_stats


def _fill(i, t, action, qty, price, position, is_entry, is_exit, *,
          tmpl="MES_SCALP_8t_8-20", acct="DEMO", sym="MES", contract="MES JUN26",
          pv=5.0, comm=0.0, fee=0.0):
    return {
        "id": i, "time_utc": t, "order_action": action, "qty": qty, "price": price,
        "position": position, "is_entry": is_entry, "is_exit": is_exit,
        "account_name": acct, "master_symbol": sym, "symbol": contract,
        "point_value": pv, "commission": comm, "fee": fee,
        "strategy_template": tmpl, "strategy_name": "AtmStrategy",
    }


def test_short_direction_from_signed_position():
    # NT labels the ATM short's entry 'BuyToCover' with position -2; exits Sell.
    fills = [
        _fill(1, "2026-06-04T23:00:09Z", "BuyToCover", 2, 7586.0, -2, 1, 0),
        _fill(2, "2026-06-04T23:04:05Z", "Sell", 1, 7584.0, -1, 0, 1),
        _fill(3, "2026-06-04T23:07:25Z", "Sell", 1, 7584.0, 0, 0, 1),
    ]
    trades = derive_trades(fills)
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "Short"
    # short gross = (avg_entry - avg_exit) * qty * pv = (7586-7584)*2*5 = +20
    assert math.isclose(t["gross_pnl"], 20.0)
    assert t["is_scale_out"] is True
    assert len(t["exit_fills"]) == 2


def test_long_loss_scale_out_pnl():
    # The 21:15 trade: long 2 @ 7563.5, both legs stopped @ 7561.25.
    fills = [
        _fill(1, "2026-06-05T02:19:09Z", "Buy", 2, 7563.5, 2, 1, 0, comm=1.9),
        _fill(2, "2026-06-05T02:19:44Z", "SellShort", 1, 7561.25, 1, 0, 1, comm=0.95),
        _fill(3, "2026-06-05T02:19:44Z", "SellShort", 1, 7561.25, 0, 0, 1, comm=0.95),
    ]
    t = derive_trades(fills)[0]
    assert t["direction"] == "Long"
    assert math.isclose(t["gross_pnl"], -22.5)      # (7561.25-7563.5)*2*5
    assert math.isclose(t["net_pnl"], -26.3)        # minus $3.80 commissions
    assert all(f["pnl"] < 0 for f in t["exit_fills"])


def test_reversal_splits_into_two_trades():
    # +1 long, then one 2-lot order flips the position to -1 short (NT8 marks it
    # is_entry=1 AND is_exit=1), then flatten. Must be TWO qty-1 trades, not one
    # qty-3 blob (the user's MES_SCALP qty-3 bug).
    fills = [
        _fill(1, "2026-06-05T05:00:00Z", "Buy",        1, 5800.0,  1, 1, 0),
        _fill(2, "2026-06-05T05:01:00Z", "SellShort",  2, 5802.0, -1, 1, 1),  # reversal
        _fill(3, "2026-06-05T05:02:00Z", "BuyToCover", 1, 5801.0,  0, 0, 1),
    ]
    trades = derive_trades(fills)
    assert len(trades) == 2
    bydir = {t["direction"]: t for t in trades}
    assert bydir["Long"]["qty"] == 1 and bydir["Short"]["qty"] == 1
    assert math.isclose(bydir["Long"]["gross_pnl"], 10.0)   # 5800->5802 x1 x5
    assert math.isclose(bydir["Short"]["gross_pnl"], 5.0)   # 5802->5801 x1 x5


def test_compute_stats_aggregate():
    fills = [
        # winner +20
        _fill(1, "2026-06-04T23:00:09Z", "BuyToCover", 2, 7586.0, -2, 1, 0),
        _fill(2, "2026-06-04T23:07:25Z", "Sell", 2, 7584.0, 0, 0, 1),
        # loser -26.30
        _fill(3, "2026-06-05T02:19:09Z", "Buy", 2, 7563.5, 2, 1, 0, comm=1.9),
        _fill(4, "2026-06-05T02:19:44Z", "SellShort", 2, 7561.25, 0, 0, 1, comm=1.9),
    ]
    stats = compute_stats(derive_trades(fills))
    assert stats["trade_count"] == 2
    assert stats["win_count"] == 1 and stats["loss_count"] == 1
    assert stats["win_rate"] == 0.5
