"""Auto-trader one-trade-at-a-time gate.

An open scale-out RUNNER keeps its instrument locked (don't stack a new entry on
a position still held after TP1), but a fully-resolved signal frees it so trading
resumes. Keys on the signal's leg state, not the garbled raw fill position column
(which would deadlock the queue on phantom stale positions).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import dashboard.api.auto_trader as at


def _iso_ago(minutes: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_hung_detection():
    now = datetime.now()
    base_p = {"instrument": "MES", "direction": "long"}
    # hung: filled, still open (no outcome), no activity for 40 min
    h = at._hung_detail({"timestamp": "t1", "proposal": base_p,
                         "exec": {"state": "filled", "filled_at": _iso_ago(40)}}, now)
    assert h is not None and h["age_minutes"] >= 30 and h["state"] == "filled"
    # NOT hung: recently filled (still legitimately live)
    assert at._hung_detail({"timestamp": "t2", "proposal": base_p,
                            "exec": {"state": "filled", "filled_at": _iso_ago(5)}}, now) is None
    # NOT hung: resolved (terminal outcome + closed leg)
    assert at._hung_detail({"timestamp": "t3", "proposal": base_p,
                            "exec": {"state": "filled", "filled_at": _iso_ago(40)},
                            "outcome": {"result": "stop"},
                            "legs": [{"result": "stop"}]}, now) is None
    # NOT hung: an armed/never-placed signal isn't a hung TRADE
    assert at._hung_detail({"timestamp": "t4", "proposal": base_p,
                            "exec": {"state": "armed", "armed_at": _iso_ago(40)}}, now) is None
    # hung: working entry that never filled or cancelled
    h2 = at._hung_detail({"timestamp": "t5", "proposal": base_p,
                          "exec": {"state": "working", "working_at": _iso_ago(45)}}, now)
    assert h2 is not None and h2["state"] == "working"


def _filled(outcome=None, legs=None):
    rec = {"exec": {"state": "filled"}}
    if outcome is not None:
        rec["outcome"] = {"result": outcome}
    if legs is not None:
        rec["legs"] = legs
    return rec


def test_unresolved_outcome_is_open():
    assert at._trade_still_open(_filled(outcome=None)) is True
    assert at._trade_still_open(_filled(outcome="pending")) is True


def test_open_runner_leg_is_open():
    # TP1 hit (outcome='partial') but the runner leg is still open -> still locked.
    legs = [{"bracket_idx": 0, "result": "target"},
            {"bracket_idx": 1, "result": "neither"}]
    assert at._trade_still_open(_filled(outcome="partial", legs=legs)) is True
    legs2 = [{"bracket_idx": 0, "result": "target"},
             {"bracket_idx": 1, "open": True}]
    assert at._trade_still_open(_filled(outcome="partial", legs=legs2)) is True


def test_fully_resolved_trade_frees_instrument():
    # Both legs closed -> not open, so a new entry may follow.
    legs = [{"bracket_idx": 0, "result": "target"},
            {"bracket_idx": 1, "result": "stop"}]
    assert at._trade_still_open(_filled(outcome="partial", legs=legs)) is False
    assert at._trade_still_open(_filled(outcome="target", legs=[{"result": "target"}])) is False
    assert at._trade_still_open(_filled(outcome="stop")) is False


def test_false_outcome_with_open_legs_is_still_open():
    # The legs are authoritative: outcome='stop' was written falsely while price
    # never hit the stop and the position is still running (legs 'neither').
    # Must stay OPEN so the gate doesn't release a live position.
    legs = [{"bracket_idx": 0, "result": "neither"},
            {"bracket_idx": 1, "result": "neither"}]
    assert at._trade_still_open(_filled(outcome="stop", legs=legs)) is True
    assert at._trade_still_open(_filled(outcome="target", legs=legs)) is True


def test_duplicate_bar_not_placed_twice(tmp_path, monkeypatch):
    """One order per (instrument, bar): a duplicate-bar signal must NOT be offered
    when a sibling for the same bar already went out -- even after the first
    filled and closed fast, freeing the instrument (the real 14:00 MES double).
    """
    from src import signal_storage

    log = tmp_path / "signals.jsonl"
    acct = "SIMTEST"
    bar = 1780686000
    monkeypatch.setattr(at.bridge, "SIGNALS_LOG", log)
    monkeypatch.setattr(at.settings_mod, "in_blackout", lambda *a, **k: (False, None))
    monkeypatch.setattr(at.settings_mod, "auto_trader_config", lambda: SimpleNamespace(
        enabled=True, account=acct, max_concurrent=3, max_contracts_per_order=10,
        entry_window_minutes=15, enabled_at=None, min_account_balance=0))

    prop = {"instrument": "MES", "direction": "short", "entry": 7416.5,
            "atm_strategy": "MES_1c", "qty": 1}
    # Sibling #1: same bar, FILLED then CLOSED fast (resolved) -> frees instrument
    # but stays in acted_bars.
    signal_storage.append_signal(log, {
        "timestamp": "2026-06-05T14:00:09", "proposal": dict(prop),
        "headless_bar_ts": bar,
        "exec": {"state": "filled", "account": acct, "fill_price": 7416.5},
        "outcome": {"result": "target"}})
    # Sibling #2: SAME bar, armed seconds later (the duplicate dispatch).
    dup_ts = "2026-06-05T14:00:17"
    signal_storage.append_signal(log, {
        "timestamp": dup_ts, "proposal": dict(prop),
        "headless_bar_ts": bar, "arm_account": acct,
        "exec": {"state": "armed", "armed_at": datetime.now().isoformat(timespec="seconds")}})

    res = at.exec_queue(acct)

    # The duplicate is NOT offered to the strategy...
    assert all(s["ts"] != dup_ts for s in res["signals"])
    # ...and it's resolved no_fill with the duplicate-bar reason.
    rec = signal_storage.load_all(log)[dup_ts]
    assert rec["outcome"]["result"] == "no_fill"
    assert "duplicate bar" in rec["outcome"]["note"]


# ---------------------------------------------------------------------------
# Computed current cash (base_cash + realized since basis), replacing the broker
# NetLiquidation pull. Trailing-DD + risk sizing + balance floor all read it.
# ---------------------------------------------------------------------------

from dashboard.api.settings import AccountConfig  # noqa: E402


def _trade(account, exit_time, net):
    return {"account": account, "exit_time": exit_time, "net_pnl": net}


def _patch_trades(monkeypatch, fills_rows, trade_rows):
    """Stub the trades.db round-trip so tests never touch a real DB. The realized
    -since helper reuses derive_trades on fetched fills -- we replace both."""
    monkeypatch.setattr(at.db, "fetch_fills_for_derivation", lambda **k: fills_rows)
    monkeypatch.setattr(at.tradelib, "derive_trades", lambda fills: trade_rows)


def test_current_cash_none_without_basis(monkeypatch):
    # No config -> None; config with base_cash but no basis -> None.
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: None)
    assert at.current_cash("ACC") is None
    cfg = AccountConfig(base_cash=10000.0, cash_basis_ts="")
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: cfg)
    assert at.current_cash("ACC") is None


def test_current_cash_base_plus_realized_since_basis(monkeypatch):
    basis = "2026-06-10T00:00:00Z"
    cfg = AccountConfig(base_cash=10000.0, cash_basis_ts=basis)
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: cfg)
    # Two trades after basis (+150, -50), one BEFORE (ignored), one other account.
    rows = [
        _trade("ACC", "2026-06-09T23:00:00Z", 999.0),   # before basis -> ignored
        _trade("ACC", "2026-06-11T15:00:00Z", 150.0),
        _trade("ACC", "2026-06-12T15:00:00Z", -50.0),
        _trade("OTHER", "2026-06-12T16:00:00Z", 777.0),  # wrong account -> ignored
    ]
    _patch_trades(monkeypatch, [{"x": 1}], rows)
    assert at.current_cash("ACC") == 10000.0 + 100.0


def test_current_cash_no_trades_since_basis_equals_base(monkeypatch):
    cfg = AccountConfig(base_cash=25000.0, cash_basis_ts="2026-06-10T00:00:00Z")
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: cfg)
    _patch_trades(monkeypatch, [], [])
    assert at.current_cash("ACC") == 25000.0


def test_risk_sizing_uses_computed_cash(monkeypatch):
    # percent mode draws on current_cash, NOT _last_balance / NetLiquidation.
    basis = "2026-06-10T00:00:00Z"
    cfg = AccountConfig(base_cash=10000.0, cash_basis_ts=basis,
                        risk_per_trade_value=1.0, risk_per_trade_mode="percent",
                        max_contracts_per_instrument=50)
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: cfg)
    monkeypatch.setattr(at.settings_mod, "auto_trader_config",
                        lambda: SimpleNamespace(default_qty=1, max_contracts_per_order=50))
    # +5000 realized -> cash 15000; risk 1% = $150. MES tick_value 1.25, stop
    # distance 10 ticks -> per-contract risk $12.50 -> floor(150/12.5) = 12.
    _patch_trades(monkeypatch, [{"x": 1}], [_trade("ACC", "2026-06-11T00:00:00Z", 5000.0)])
    # MES: entry 5000.00, stop 4997.50 -> 10 ticks (tick 0.25).
    qty, reason = at._resolved_qty(
        {"instrument": "MES", "entry": 5000.0, "stop": 4997.50}, "ACC")
    assert qty == 12, reason


def test_trailing_dd_breach_on_computed_cash(monkeypatch):
    at._equity_hwm.clear()
    basis = "2026-06-10T00:00:00Z"
    cfg = AccountConfig(base_cash=10000.0, cash_basis_ts=basis, trailing_dd_limit=500.0)
    monkeypatch.setattr(at.settings_mod, "account_config", lambda a: cfg)
    # First read: +600 -> cash 10600 sets HWM 10600, no breach.
    _patch_trades(monkeypatch, [{"x": 1}], [_trade("ACC", "2026-06-11T00:00:00Z", 600.0)])
    s1 = at._trailing_dd_state("ACC")
    assert s1["high_water_mark"] == 10600.0 and s1["dd_breached"] is False
    # Then a drawdown: cash falls to 10050 -> used 550 >= 500 limit -> breached.
    _patch_trades(monkeypatch, [{"x": 1}], [_trade("ACC", "2026-06-12T00:00:00Z", 50.0)])
    s2 = at._trailing_dd_state("ACC")
    assert s2["trailing_dd_used"] == 550.0 and s2["dd_breached"] is True
