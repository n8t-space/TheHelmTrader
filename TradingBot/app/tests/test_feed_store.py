"""Tests for src.feed_store.

Each test runs against a temp SQLite file (not the production feed.db).
We monkeypatch the module's DB_PATH and reset the cached connection.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Fresh feed.db per test, in tmp_path."""
    from src import feed_store

    test_db = tmp_path / "feed.db"
    monkeypatch.setattr(feed_store, "DB_PATH", test_db)
    monkeypatch.setattr(feed_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(feed_store, "_conn", None)  # force re-open
    feed_store.init_schema()
    yield feed_store
    # Cleanup connection so the next test starts clean
    if feed_store._conn is not None:
        feed_store._conn.close()
    monkeypatch.setattr(feed_store, "_conn", None)


def _connect(path):
    return sqlite3.connect(path)


# ---------------- Schema ----------------

def test_init_schema_creates_three_tables(store, tmp_path):
    conn = _connect(tmp_path / "feed.db")
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"bars", "ticks", "auto_analysis_config"}.issubset(tables)


def test_init_schema_idempotent(store):
    store.init_schema()  # second call must not throw
    store.init_schema()


# ---------------- Bars ----------------

def test_insert_bar_then_read_back(store, tmp_path):
    store.insert_bar("MES", "5m", 1715184000, 5800.0, 5805.0, 5798.0, 5803.0, 12345)
    rows = list(_connect(tmp_path / "feed.db").execute("SELECT * FROM bars"))
    assert rows == [("MES", "5m", 1715184000, 5800.0, 5805.0, 5798.0, 5803.0, 12345)]


def test_insert_bar_pk_replaces_on_collision(store, tmp_path):
    store.insert_bar("MES", "5m", 1715184000, 5800, 5805, 5798, 5803, 1)
    store.insert_bar("MES", "5m", 1715184000, 5810, 5820, 5805, 5815, 9)
    rows = list(_connect(tmp_path / "feed.db").execute("SELECT * FROM bars"))
    assert len(rows) == 1
    assert rows[0][3:] == (5810.0, 5820.0, 5805.0, 5815.0, 9)  # latest wins


# ---------------- Ticks ----------------

def test_insert_ticks_dedupes_on_pk(store, tmp_path):
    store.insert_ticks([
        ("MES", 1715184300100, 5805.0, 1),
        ("MES", 1715184300100, 5805.0, 1),  # exact dup; should be silently dropped
        ("MES", 1715184300250, 5805.25, 3),
    ])
    rows = sorted(_connect(tmp_path / "feed.db").execute("SELECT * FROM ticks").fetchall())
    assert rows == [
        ("MES", 1715184300100, 5805.0, 1),
        ("MES", 1715184300250, 5805.25, 3),
    ]


def test_insert_ticks_empty_is_noop(store):
    store.insert_ticks([])  # must not raise


# ---------------- Auto-analysis config ----------------

def test_replace_config_round_trip(store):
    store.replace_config([
        {"instrument": "MES", "period": "5m",  "enabled": True},
        {"instrument": "MCL", "period": "5m",  "enabled": True},
        {"instrument": "NQ",  "period": "15m", "enabled": False},
    ])
    cfg = store.list_config()
    assert {(c["instrument"], c["period"], c["enabled"]) for c in cfg} == {
        ("MES", "5m", True), ("MCL", "5m", True), ("NQ", "15m", False),
    }


def test_replace_config_with_empty_clears_table(store):
    store.replace_config([{"instrument": "X", "period": "1m", "enabled": True}])
    store.replace_config([])
    assert store.list_config() == []


def test_is_armed_true_only_for_enabled_pair(store):
    store.replace_config([
        {"instrument": "MES", "period": "5m", "enabled": True},
        {"instrument": "NQ",  "period": "15m","enabled": False},
    ])
    assert store.is_armed("MES", "5m") is True
    assert store.is_armed("NQ",  "15m") is False
    assert store.is_armed("MCL", "5m") is False  # never configured


# ---------------- Prune ----------------

def test_prune_drops_old_bars_and_ticks(store):
    store.insert_bar("MES", "5m", 1000, 1, 2, 0, 1, 1)        # very old
    store.insert_bar("MES", "5m", 99999999999, 1, 2, 0, 1, 1)  # future
    store.insert_ticks([
        ("MES", 1000_000, 1.0, 1),         # very old
        ("MES", 99999999999_000, 1.0, 1),  # future
    ])
    result = store.prune(retention_days=7)
    assert result["bars_deleted"] == 1
    assert result["ticks_deleted"] == 1


def test_prune_protected_ts_extends_retention(store):
    # Open trade from a year ago — should keep that data alive.
    very_old_ts = 1500000000  # ~mid-2017
    store.insert_bar("MES", "5m", very_old_ts, 1, 2, 0, 1, 1)
    result = store.prune(retention_days=7, protected_ts_seconds=very_old_ts - 1)
    assert result["bars_deleted"] == 0  # protected


# ---------------- Schema versioning ----------------

def test_schema_version_stamped_on_first_init(store):
    assert store.get_schema_version() == store.SCHEMA_VERSION


def test_schema_version_higher_on_disk_raises(store, tmp_path, monkeypatch):
    # Force-bump the on-disk version to "future" and re-open
    conn = sqlite3.connect(tmp_path / "feed.db")
    conn.execute("UPDATE schema_meta SET value=? WHERE key='version'", ("999",))
    conn.commit()
    conn.close()

    # Reset the cached connection so init_schema re-opens
    monkeypatch.setattr(store, "_conn", None)
    with pytest.raises(store.SchemaVersionMismatchError):
        store.init_schema()
