"""Tests for the /api/auto-analysis/* config + status endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.api.main import app
from src import feed_store

# Live integration: spawns the auto-analysis worker against real state. Slow +
# timing coupled -> not part of the fast pre-push gate. Run with `-m integration`.
pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    snap = feed_store.list_config()
    with TestClient(app) as c:
        yield c
    feed_store.replace_config(snap)


def test_get_config_returns_entries_list(client):
    r = client.get("/api/auto-analysis/config")
    assert r.status_code == 200
    assert "entries" in r.json()


def test_put_config_round_trip(client):
    r = client.put("/api/auto-analysis/config", json={"entries": [
        {"instrument": "MES", "period": "5m", "enabled": True},
        {"instrument": "MCL", "period": "5m", "enabled": True},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2

    # Round-trip
    r2 = client.get("/api/auto-analysis/config")
    keys = {(e["instrument"], e["period"]) for e in r2.json()["entries"]}
    assert ("MES", "5m") in keys
    assert ("MCL", "5m") in keys


def test_put_config_rejects_more_than_4_armed(client):
    r = client.put("/api/auto-analysis/config", json={"entries": [
        {"instrument": "A", "period": "1m", "enabled": True},
        {"instrument": "B", "period": "1m", "enabled": True},
        {"instrument": "C", "period": "1m", "enabled": True},
        {"instrument": "D", "period": "1m", "enabled": True},
        {"instrument": "E", "period": "1m", "enabled": True},
    ]})
    assert r.status_code == 400


def test_put_config_rejects_duplicate_keys(client):
    r = client.put("/api/auto-analysis/config", json={"entries": [
        {"instrument": "X", "period": "1m", "enabled": True},
        {"instrument": "X", "period": "1m", "enabled": False},
    ]})
    assert r.status_code == 400


def test_status_returns_expected_shape(client):
    r = client.get("/api/auto-analysis/status")
    assert r.status_code == 200
    body = r.json()
    assert "queue_size" in body
    assert "run_count" in body
    assert "worker_alive" in body
