"""Auto-trader one-trade-at-a-time gate.

An open scale-out RUNNER keeps its instrument locked (don't stack a new entry on
a position still held after TP1), but a fully-resolved signal frees it so trading
resumes. Keys on the signal's leg state, not the garbled raw fill position column
(which would deadlock the queue on phantom stale positions).
"""
from __future__ import annotations

import dashboard.api.auto_trader as at


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


def test_terminal_outcome_with_stale_neither_legs_is_closed():
    # outcome=stop but legs left at 'neither' (feed gap) -> still CLOSED, must not
    # deadlock the instrument. Only 'partial' consults the legs.
    legs = [{"bracket_idx": 0, "result": "neither"},
            {"bracket_idx": 1, "result": "neither"}]
    assert at._trade_still_open(_filled(outcome="stop", legs=legs)) is False
    assert at._trade_still_open(_filled(outcome="target", legs=legs)) is False
