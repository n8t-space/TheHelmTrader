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
