"""Signal <-> NT fill reconciliation core.

Asserts the ground-truth contract: a confidently-linked mismatch is corrected to
the broker net, an agreeing trade is left alone, and a filled-but-unlinked signal
is flagged 'unverified' -- never assigned a guessed number.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

import dashboard.api.auditor as auditor
from src import instruments

CONFIG = instruments.load_config()


@pytest.fixture
def captured(monkeypatch):
    """Capture writes instead of touching signals.jsonl / audit_log.jsonl."""
    updates: list[tuple[str, dict]] = []
    logs: list[dict] = []
    monkeypatch.setattr(auditor.signal_storage, "append_update",
                        lambda path, ts, **f: updates.append((ts, f)))
    monkeypatch.setattr(auditor, "_append_log", lambda e: logs.append(e))
    return {"updates": updates, "logs": logs}


def _filled_long_signal(ts="2026-06-04T21:15:05"):
    return {
        "timestamp": ts,
        "proposal": {"direction": "long", "instrument": "MES",
                     "entry": 7563.5, "stop": 7561.5, "target": 7568.5,
                     "atm_strategy": "MES_SCALP_8t_8-20", "atm_total_qty": 2},
        # paper resolver booked a +11.25 "win"
        "legs": [
            {"bracket_idx": 0, "qty": 1, "exit_price": 7565.5, "result": "target"},
            {"bracket_idx": 1, "qty": 1, "exit_price": 7563.75, "result": "be"},
        ],
        "exec": {"state": "filled", "account": "DEMO", "filled_at": ts},
    }


def _trade(net=-26.3, gross=-22.5, qty=2):
    return {
        "account": "DEMO", "symbol": "MES", "direction": "Long",
        "qty": qty, "entry_price": 7563.5, "exit_time": "2026-06-05T02:19:44Z",
        "net_pnl": net, "gross_pnl": gross,
        "exit_fills": [
            {"time": "2026-06-05T02:19:44Z", "qty": 1, "price": 7561.25, "pnl": -11.25},
            {"time": "2026-06-05T02:19:44Z", "qty": 1, "price": 7561.25, "pnl": -11.25},
        ],
    }


def test_corrects_paper_win_to_real_loss(captured):
    sig = _filled_long_signal()
    signals = {sig["timestamp"]: sig}
    links = {sig["timestamp"]: {"trade": _trade(), "confidence": 1.0}}

    summary = auditor.reconcile(signals, links, CONFIG)

    assert summary["corrected"] == 1
    assert summary["unverified"] == 0
    ts, fields = captured["updates"][0]
    audit = fields["audit"]
    assert audit["source"] == "fills"
    assert math.isclose(audit["realized_pnl"], -26.3)
    assert audit["real_qty"] == 2
    # legs replaced with the real exit fills
    assert all(leg["engine"] == "auditor" for leg in fields["legs"])
    # aggregate outcome stamped so Signal Analysis shows the trade CLOSED
    assert fields["outcome"]["result"] == "stop"           # both legs lost
    assert fields["outcome"]["auditor_resolved"] is True
    assert fields["entry_triggered"] is True
    # one immutable log line recording paper -> fills
    assert captured["logs"][0]["new_realized"] == -26.3


def test_backfills_closed_outcome_when_pnl_already_correct(captured):
    """A signal whose P&L already matches fills but whose outcome was never
    stamped (paper left the runner open) must get the closed outcome backfilled,
    without being re-counted as a correction."""
    sig = _filled_long_signal()
    sig["audit"] = {"source": "fills", "realized_pnl": 33.4}   # already corrected
    sig["outcome"] = None                                      # but never closed
    trade = _trade(net=33.4, gross=37.5, qty=2)
    trade["exit_fills"] = [
        {"time": "t1", "qty": 1, "price": 7566.0, "pnl": 12.5},
        {"time": "t2", "qty": 1, "price": 7568.5, "pnl": 20.9},
    ]
    signals = {sig["timestamp"]: sig}
    links = {sig["timestamp"]: {"trade": trade, "confidence": 1.0}}

    summary = auditor.reconcile(signals, links, CONFIG)
    assert summary["corrected"] == 0          # P&L unchanged -> not a correction
    assert summary["in_sync"] == 1
    _, fields = captured["updates"][0]
    assert fields["outcome"]["result"] == "target"
    assert fields["entry_triggered"] is True
    assert captured["logs"] == []             # no correction logged


def test_in_sync_when_paper_matches_fills(captured):
    sig = _filled_long_signal()
    # paper legs total -22.50 gross; metrics net the est. fee (MES $1.10 x2) ->
    # paper net -24.70. For 'in sync' the trade's real net must equal that, and
    # the watcher already stamped the closed outcome -> nothing for the auditor.
    sig["legs"] = [
        {"bracket_idx": 0, "qty": 1, "exit_price": 7561.25, "result": "stop"},
        {"bracket_idx": 1, "qty": 1, "exit_price": 7561.25, "result": "stop"},
    ]
    sig["outcome"] = {"result": "stop"}
    signals = {sig["timestamp"]: sig}
    links = {sig["timestamp"]: {"trade": _trade(net=-24.7, gross=-22.5), "confidence": 1.0}}

    summary = auditor.reconcile(signals, links, CONFIG)
    assert summary["in_sync"] == 1
    assert summary["corrected"] == 0
    assert captured["updates"] == []          # already closed + P&L matches


def test_unlinked_filled_signal_is_flagged_not_guessed(captured):
    sig = _filled_long_signal()
    signals = {sig["timestamp"]: sig}
    links: dict = {}  # no confident match

    summary = auditor.reconcile(signals, links, CONFIG)
    assert summary["unverified"] == 1
    assert summary["corrected"] == 0
    ts, fields = captured["updates"][0]
    assert fields["audit"]["source"] == "unlinked"
    assert "realized_pnl" not in fields["audit"]   # never a guessed number
    assert captured["logs"] == []                  # corrections log untouched


def test_unfilled_signal_is_skipped(captured):
    sig = _filled_long_signal()
    sig["exec"] = {"state": "working"}             # not filled -> not auditable
    summary = auditor.reconcile({sig["timestamp"]: sig}, {}, CONFIG)
    assert summary["checked"] == 0
    assert captured["updates"] == []


def test_stuck_runner_resolves_all_legs_from_fills(captured):
    """The core multi-contract complaint: paper banked leg0 (target) and froze
    the runner 'neither', so the signal showed 'first win only'. The auditor must
    resolve BOTH legs from the real fills, like Trade Performance."""
    sig = _filled_long_signal()
    sig["legs"] = [
        {"bracket_idx": 0, "qty": 1, "exit_price": 7565.5, "result": "target"},
        {"bracket_idx": 1, "qty": 1, "exit_price": None, "result": "neither"},
    ]  # runner stuck open
    trade = _trade(net=33.4, gross=37.5, qty=2)
    trade["exit_fills"] = [
        {"time": "t1", "qty": 1, "price": 7566.0, "pnl": 12.5},
        {"time": "t2", "qty": 1, "price": 7568.5, "pnl": 25.0},
    ]
    signals = {sig["timestamp"]: sig}
    links = {sig["timestamp"]: {"trade": trade, "confidence": 1.0}}

    summary = auditor.reconcile(signals, links, CONFIG)
    assert summary["corrected"] == 1
    _, fields = captured["updates"][0]
    legs = fields["legs"]
    assert len(legs) == 2                       # both legs, not just the winner
    assert [l["bracket_idx"] for l in legs] == [0, 1]
    assert [l["pnl"] for l in legs] == [12.5, 25.0]
    assert all(l["result"] == "target" for l in legs)
    assert math.isclose(fields["audit"]["realized_pnl"], 33.4)


def test_filled_within_recency_window():
    now_sig = _filled_long_signal()
    now_sig["exec"]["filled_at"] = datetime.now().isoformat(timespec="seconds")
    old_sig = _filled_long_signal()
    old_sig["exec"]["filled_at"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    working = _filled_long_signal()
    working["exec"] = {"state": "working"}

    assert auditor._filled_within(now_sig, auditor.RECENT_FILL_WINDOW_S) is True
    assert auditor._filled_within(old_sig, auditor.RECENT_FILL_WINDOW_S) is False
    assert auditor._filled_within(working, auditor.RECENT_FILL_WINDOW_S) is False
