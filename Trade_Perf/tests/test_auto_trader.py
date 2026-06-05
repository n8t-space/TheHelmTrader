"""Auto-trader one-trade-at-a-time gate.

Locks the rule that an open scale-out RUNNER keeps its instrument locked: a new
entry must not stack on a position the account still holds, even after TP1 has
set the signal outcome to 'partial'.
"""
from __future__ import annotations

import dashboard.api.auto_trader as at
from dashboard.api import db


def test_open_position_detects_runner(monkeypatch):
    fills = [
        # MES: last fill still holds 1 (a runner) -> open
        {"account_name": "A", "master_symbol": "MES", "time_utc": "t1", "id": 1, "position": 2},
        {"account_name": "A", "master_symbol": "MES", "time_utc": "t2", "id": 2, "position": 1},
        # MCL: returns to flat -> closed
        {"account_name": "A", "master_symbol": "MCL", "time_utc": "t1", "id": 3, "position": 1},
        {"account_name": "A", "master_symbol": "MCL", "time_utc": "t2", "id": 4, "position": 0},
        # different account -> ignored
        {"account_name": "B", "master_symbol": "CL", "time_utc": "t1", "id": 5, "position": 3},
    ]
    monkeypatch.setattr(db, "fetch_fills_for_derivation", lambda account=None: fills)
    assert at._instruments_with_open_position("A") == {"MES"}


def test_flat_account_has_no_open_instruments(monkeypatch):
    fills = [
        {"account_name": "A", "master_symbol": "MES", "time_utc": "t1", "id": 1, "position": 2},
        {"account_name": "A", "master_symbol": "MES", "time_utc": "t2", "id": 2, "position": 0},
    ]
    monkeypatch.setattr(db, "fetch_fills_for_derivation", lambda account=None: fills)
    assert at._instruments_with_open_position("A") == set()


def test_open_position_lookup_fails_open(monkeypatch):
    def boom(account=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "fetch_fills_for_derivation", boom)
    # Must not raise -> empty set so the queue isn't deadlocked.
    assert at._instruments_with_open_position("A") == set()
