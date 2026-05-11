"""End-to-end tests for the /api/feed/* routes — warmup gate, arming
predicate, prune endpoint, and Pydantic validation.

These exercise the production feed.db (whatever's at TradingBot/app/data/),
so they snapshot + restore the auto_analysis_config table around each test
and clean up any test bars they wrote.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.api.main import app
import dashboard.api.feed as feed_mod
from src import auto_analyzer, feed_store


# ---------------- fixtures ----------------

@pytest.fixture
def client():
    """Single TestClient + reset module-level state per test."""
    feed_mod._last_bar_ts.clear()
    snap = feed_store.list_config()
    with TestClient(app) as c:
        yield c
    feed_store.replace_config(snap)


# ---------------- warmup gate ----------------

BASE_TS = 1_715_200_000  # arbitrary unix s


def _bar(client, ts, instrument="WGT_TEST", period="5m"):
    return client.post("/api/feed/bar", json={
        "instrument": instrument, "period": period, "ts": ts,
        "o": 1, "h": 1, "l": 1, "c": 1, "v": 1,
    })


def test_first_bar_after_startup_is_warmup_skip(client):
    feed_store.replace_config([
        {"instrument": "WGT_TEST", "period": "5m", "enabled": True}])
    r = _bar(client, BASE_TS)
    assert r.status_code == 200
    body = r.json()
    assert body["armed"] is False
    assert body["reason"] == "post-gap warmup"


def test_second_bar_within_window_is_armed(client):
    feed_store.replace_config([
        {"instrument": "WGT_TEST", "period": "5m", "enabled": True}])
    _bar(client, BASE_TS)                  # warmup
    r = _bar(client, BASE_TS + 300)        # 5m later → armed
    assert r.json()["armed"] is True


def test_post_30min_gap_is_warmup_skip(client):
    feed_store.replace_config([
        {"instrument": "WGT_TEST", "period": "5m", "enabled": True}])
    _bar(client, BASE_TS)
    _bar(client, BASE_TS + 300)
    r = _bar(client, BASE_TS + 300 + 31 * 60)   # >30 min gap
    assert r.json()["armed"] is False
    assert r.json()["reason"] == "post-gap warmup"


def test_out_of_order_bar_does_not_regress_last_ts(client):
    feed_store.replace_config([
        {"instrument": "WGT_TEST", "period": "5m", "enabled": True}])
    _bar(client, BASE_TS)
    _bar(client, BASE_TS + 300)
    pre = feed_mod._last_bar_ts["WGT_TEST"]
    _bar(client, BASE_TS)                  # backwards bar
    assert feed_mod._last_bar_ts["WGT_TEST"] == pre


def test_unarmed_pair_never_triggers(client):
    # No config rows at all
    feed_store.replace_config([])
    _bar(client, BASE_TS)
    r = _bar(client, BASE_TS + 300)
    assert r.json()["armed"] is False


# ---------------- prune endpoint ----------------

def test_prune_endpoint_returns_counts(client):
    r = client.post("/api/feed/prune?retention_days=99999")  # huge retention -> 0
    assert r.status_code == 200
    body = r.json()
    assert "cutoff_s" in body
    assert "bars_deleted" in body
    assert "ticks_deleted" in body
    assert body["bars_deleted"] == 0
    assert body["ticks_deleted"] == 0


# ---------------- pydantic validation ----------------

def test_bad_bar_type_returns_422(client):
    r = client.post("/api/feed/bar", json={
        "instrument": "WGT_TEST", "period": "5m", "ts": "not-an-int",
        "o": 1, "h": 1, "l": 1, "c": 1, "v": 1,
    })
    assert r.status_code == 422


def test_empty_tick_batch_ok(client):
    r = client.post("/api/feed/ticks", json={"instrument": "WGT_TEST", "ticks": []})
    assert r.status_code == 200
    assert r.json()["count"] == 0
