"""Automation blackout-window logic (settings.in_blackout)."""
from __future__ import annotations

from datetime import datetime

import dashboard.api.settings as st


def _set_windows(monkeypatch, windows):
    s = st.Settings()
    s.automation.blackout_windows = [st.BlackoutWindow(**w) for w in windows]
    monkeypatch.setattr(st, "_cache", s)


def test_no_windows_never_blacks_out(monkeypatch):
    _set_windows(monkeypatch, [])
    assert st.in_blackout(datetime(2026, 6, 5, 12, 0))[0] is False


def test_daytime_window(monkeypatch):
    _set_windows(monkeypatch, [{"start": "11:00", "end": "13:00", "label": "lunch"}])
    inside, label = st.in_blackout(datetime(2026, 6, 5, 12, 0))
    assert inside is True and label == "lunch"
    assert st.in_blackout(datetime(2026, 6, 5, 10, 59))[0] is False
    assert st.in_blackout(datetime(2026, 6, 5, 13, 0))[0] is False   # end exclusive


def test_overnight_window_spans_midnight(monkeypatch):
    _set_windows(monkeypatch, [{"start": "22:00", "end": "06:00"}])
    assert st.in_blackout(datetime(2026, 6, 5, 23, 0))[0] is True
    assert st.in_blackout(datetime(2026, 6, 5, 3, 0))[0] is True
    assert st.in_blackout(datetime(2026, 6, 5, 12, 0))[0] is False
    assert st.in_blackout(datetime(2026, 6, 5, 6, 0))[0] is False    # end exclusive


def test_multiple_windows(monkeypatch):
    _set_windows(monkeypatch, [
        {"start": "08:30", "end": "08:45", "label": "open"},
        {"start": "11:30", "end": "12:30", "label": "lunch"},
    ])
    assert st.in_blackout(datetime(2026, 6, 5, 8, 35))[0] is True
    assert st.in_blackout(datetime(2026, 6, 5, 12, 0))[0] is True
    assert st.in_blackout(datetime(2026, 6, 5, 9, 30))[0] is False
