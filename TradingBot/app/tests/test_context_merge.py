"""Phase 1 of the HelmFeed/HelmAnalyzer merge: the shared context formatter
and the headless NS-context reader (bar_ts-gated, with thin fallback)."""
from __future__ import annotations

import json

from src import headless_analyzer
from src.context_format import format_ns_context

_NS_CTX = {
    "instrument": "MES",
    "current": {"bid": 5000.25, "ask": 5000.5, "last": 5000.5},
    "timeframes": {"primary": {"ema90": 4990.0, "adxr": 27.4,
                               "donchian_upper": 5010.0, "donchian_lower": 4980.0}},
    "daily_levels": {"pivot_p": 4995.0, "today_high": 5012.0, "today_low": 4975.0},
    "market_structure": [
        {"retrace_pct": 1.0, "trend": "Up", "structure": "Bullish",
         "last_structure_event": "BullishBOS", "break_price": 4998.0,
         "last_confirmed_high": {"label": "HH", "price": 5010.0},
         "last_confirmed_low":  {"label": "HL", "price": 4985.0}},
    ],
}


def test_formatter_renders_structure_and_truth_footer():
    block = format_ns_context(_NS_CTX)
    assert "Market structure" in block
    assert "BullishBOS" in block
    assert "HH" in block and "HL" in block
    assert "adxr=27.4" in block
    assert "VERIFIED source of truth" in block


def test_formatter_omits_structure_when_absent():
    block = format_ns_context({"instrument": "MES", "current": {"last": 1}})
    assert "Market structure" not in block


def _write_ctx(tmp_path, instrument, period, bar_ts, ctx):
    p = tmp_path / f"context_{instrument}_{period}.json"
    p.write_text(json.dumps({"bar_ts": bar_ts, "context": ctx}), encoding="utf-8")
    return p


def test_ns_context_used_when_bar_ts_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(headless_analyzer, "SCREENSHOTS_DIR", tmp_path)
    _write_ctx(tmp_path, "MES", "15m", 1780686000, _NS_CTX)
    got = headless_analyzer._latest_auto_context("MES", "15m", 1780686000)
    assert got is not None
    assert got["market_structure"][0]["last_structure_event"] == "BullishBOS"


def test_ns_context_rejected_on_bar_ts_mismatch(tmp_path, monkeypatch):
    # A newer bar's context must never leak into an older bar's analysis.
    monkeypatch.setattr(headless_analyzer, "SCREENSHOTS_DIR", tmp_path)
    _write_ctx(tmp_path, "MES", "15m", 1780686900, _NS_CTX)
    assert headless_analyzer._latest_auto_context("MES", "15m", 1780686000) is None


def test_ns_context_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(headless_analyzer, "SCREENSHOTS_DIR", tmp_path)
    assert headless_analyzer._latest_auto_context("MES", "15m", 1780686000) is None
