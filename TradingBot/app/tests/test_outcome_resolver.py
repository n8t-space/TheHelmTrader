"""Tests for src.outcome_resolver — the 12 cases from the inline 2026-05-09 run."""
from __future__ import annotations

import sqlite3

import pytest

from src.outcome_resolver import resolve_outcome


# ---------- fixtures ----------

BARS_DDL = """
CREATE TABLE bars (
    instrument TEXT NOT NULL, period TEXT NOT NULL, ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v INTEGER,
    PRIMARY KEY (instrument, period, ts)
)"""

TICKS_DDL = """
CREATE TABLE ticks (
    instrument TEXT NOT NULL, ts_ms INTEGER NOT NULL,
    price REAL NOT NULL, volume INTEGER NOT NULL,
    PRIMARY KEY (instrument, ts_ms, price)
) WITHOUT ROWID"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(BARS_DDL)
    c.execute(TICKS_DDL)
    yield c
    c.close()


def add_bar(c, inst, period, ts, o, h, l, cl, v=100):
    c.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)",
              (inst, period, ts, o, h, l, cl, v))


def add_tick(c, inst, ts_ms, price, vol=1):
    c.execute("INSERT OR IGNORE INTO ticks VALUES(?,?,?,?)",
              (inst, ts_ms, price, vol))


ENTRY = 1715184000          # arbitrary unix s
ENTRY_MS = ENTRY * 1000


# ---------- happy paths ----------

def test_long_target_via_tick(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5807, 5798, 5806)
    add_tick(conn, "MES", ENTRY_MS + 60_000, 5805)
    r = resolve_outcome("MES", "long", ENTRY, target=5805, stop=5790, conn=conn)
    assert r == {"outcome": "target", "method": "tick",
                 "hit_ts": ENTRY_MS + 60_000, "hit_price": 5805}


def test_long_stop_via_tick(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5803, 5788, 5790)
    add_tick(conn, "MES", ENTRY_MS + 90_000, 5790)
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "stop" and r["method"] == "tick" and r["hit_price"] == 5790


def test_short_target_via_tick(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5803, 5789, 5790)
    add_tick(conn, "MES", ENTRY_MS + 90_000, 5789)
    r = resolve_outcome("MES", "short", ENTRY, target=5790, stop=5810, conn=conn)
    assert r["outcome"] == "target" and r["method"] == "tick" and r["hit_price"] == 5789


def test_neither(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5803, 5798, 5801)
    add_bar(conn, "MES", "5m", ENTRY + 600, 5801, 5804, 5799, 5802)
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r == {"outcome": "neither", "method": None, "hit_ts": None, "hit_price": None}


# ---------- ambiguous bars ----------

def test_ambiguous_bar_target_first(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5810, 5790, 5800)  # straddles both
    add_tick(conn, "MES", ENTRY_MS + 30_000, 5805, 1)
    add_tick(conn, "MES", ENTRY_MS + 60_000, 5810, 1)   # target tick first
    add_tick(conn, "MES", ENTRY_MS + 120_000, 5790, 1)  # stop tick later
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "target" and r["hit_ts"] == ENTRY_MS + 60_000


def test_ambiguous_bar_stop_first(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5810, 5790, 5800)
    add_tick(conn, "MES", ENTRY_MS + 30_000, 5790, 1)   # stop first
    add_tick(conn, "MES", ENTRY_MS + 60_000, 5810, 1)
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "stop" and r["hit_ts"] == ENTRY_MS + 30_000


def test_same_ts_tie_stop_wins(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5810, 5790, 5800)
    add_tick(conn, "MES", ENTRY_MS + 60_000, 5810)  # target
    add_tick(conn, "MES", ENTRY_MS + 60_000, 5790)  # stop, same ms
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "stop" and r["hit_price"] == 5790


# ---------- bar-only fallback ----------

def test_bar_only_target_no_ticks(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5807, 5798, 5806)
    r = resolve_outcome("MES", "long", ENTRY, target=5805, stop=5790, conn=conn)
    assert r["outcome"] == "target" and r["method"] == "bar" and r["hit_price"] == 5805


def test_ambiguous_bar_no_ticks_tie_break_stop(conn):
    add_bar(conn, "MES", "5m", ENTRY + 300, 5800, 5810, 5790, 5800)
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "stop" and r["method"] == "bar" and r["hit_price"] == 5790


# ---------- edge cases ----------

def test_no_bars(conn):
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "neither" and r["method"] is None


def test_pre_entry_bars_ignored(conn):
    # bar BEFORE entry that would cross both — must be ignored
    add_bar(conn, "MES", "5m", ENTRY - 300, 5800, 5820, 5780, 5800)
    r = resolve_outcome("MES", "long", ENTRY, target=5810, stop=5790, conn=conn)
    assert r["outcome"] == "neither"


def test_invalid_direction_raises(conn):
    with pytest.raises(ValueError):
        resolve_outcome("MES", "sideways", ENTRY, target=5810, stop=5790, conn=conn)
