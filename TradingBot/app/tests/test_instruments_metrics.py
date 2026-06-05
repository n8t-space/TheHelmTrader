"""compute_trade_metrics: sizing, multi-leg P&L, and the auditor override.

These lock the trading-correctness fixes:
  * a multi-bracket ATM is sized off its real legs, not a stale position_size,
  * the integrity auditor's real-fill P&L outranks the paper leg walk,
  * a leg's own stored P&L is trusted over a recompute off the planned entry.
"""
from __future__ import annotations

import math

from src import instruments

CONFIG = instruments.load_config()


def _long_mes(entry=7563.5, stop=7561.5, target=7568.5):
    return {
        "proposal": {
            "direction": "long", "instrument": "MES",
            "entry": entry, "stop": stop, "target": target,
            "atm_strategy": "MES_SCALP_8t_8-20", "atm_total_qty": 2,
        },
    }


def test_mes_point_value_and_tick():
    assert instruments.lookup_point_value("MES", CONFIG) == 5.0
    tick, src = instruments.lookup_tick_size("MES JUN26", CONFIG)
    assert tick == 0.25 and src == "explicit"


def test_multi_leg_sizing_from_legs():
    rec = _long_mes()
    rec["legs"] = [
        {"bracket_idx": 0, "qty": 1, "exit_price": 7565.5, "result": "target"},
        {"bracket_idx": 1, "qty": 1, "exit_price": 7563.75, "result": "be"},
    ]
    m = instruments.compute_trade_metrics(rec, CONFIG)
    # 2 contracts, not the absent/stale position_size.
    assert m["position_size"] == 2
    # risk 2 pts * $5 * 2 = $20 ; reward 5 pts * $5 * 2 = $50
    assert math.isclose(m["total_risk"], 20.0)
    assert math.isclose(m["total_reward"], 50.0)
    # realized from legs: (7565.5-7563.5)*5 + (7563.75-7563.5)*5 = 10 + 1.25
    assert math.isclose(m["realized_pnl"], 11.25)
    assert m["realized_pnl_source"] == "legs"


def test_audit_override_beats_paper_legs():
    """The 21:15 case: paper legs say +11.25, fills say it stopped for -26.30."""
    rec = _long_mes()
    rec["legs"] = [
        {"bracket_idx": 0, "qty": 1, "exit_price": 7565.5, "result": "target"},
        {"bracket_idx": 1, "qty": 1, "exit_price": 7563.75, "result": "be"},
    ]
    rec["audit"] = {"source": "fills", "realized_pnl": -26.30, "real_qty": 2}
    m = instruments.compute_trade_metrics(rec, CONFIG)
    assert math.isclose(m["realized_pnl"], -26.30)
    assert m["realized_pnl_source"] == "fills"
    assert m["position_size"] == 2


def test_stored_leg_pnl_is_trusted():
    rec = _long_mes()
    # Auditor-written legs carry their own exact dollar P&L from real fills.
    rec["legs"] = [
        {"bracket_idx": 0, "qty": 1, "exit_price": 7561.25, "result": "stop",
         "pnl": -11.25, "engine": "auditor"},
        {"bracket_idx": 1, "qty": 1, "exit_price": 7561.25, "result": "stop",
         "pnl": -11.25, "engine": "auditor"},
    ]
    m = instruments.compute_trade_metrics(rec, CONFIG)
    # Sum of stored leg pnl, not a recompute off entry=7563.5.
    assert math.isclose(m["realized_pnl"], -22.50)
    assert m["leg_breakdown"][0]["pnl"] == -11.25


def test_flat_signal_has_no_pnl():
    rec = {"proposal": {"direction": "flat", "instrument": "MES",
                        "entry": 0, "stop": 0, "target": 0}}
    m = instruments.compute_trade_metrics(rec, CONFIG)
    assert m["realized_pnl"] is None
    assert m["total_risk"] == 0.0


def test_tick_snap_rounds_to_increment():
    assert instruments.snap_to_tick(7563.58, 0.25) == 7563.5
    p = {"direction": "long", "instrument": "MES",
         "entry": 7563.58, "stop": 7561.49, "target": 7568.5}
    instruments.apply_tick_rounding(p, CONFIG)
    assert p["entry"] == 7563.5
    assert p["tick_source"] == "explicit"
    assert any(a["field"] == "entry" for a in p["tick_adjustments"])
