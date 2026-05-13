"""Was a signal's entry price touched after its timestamp?

A separate concern from outcome_resolver: that one determines whether
target or stop got hit AFTER the trade was entered. This one decides
whether the trade was ever entered at all -- i.e. whether the LLM's
entry price was touched within a reasonable window.

Three states:
    hit       — a bar at-or-after entry_ts had price cross the entry level
    no_entry  — entry_ts + max_window_s has passed and no bar touched it
    pending   — still within the window; check again next pass

Symmetric for long and short: "touched" = bar low <= entry <= bar high.
At the tick level: first tick whose price equals the entry (within
half-tick tolerance) wins. If no ticks are stored for the crossing bar,
falls back to the bar's close ts.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Literal, TypedDict

from . import outcome_resolver  # reuse _pick_finest_period

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FEED_DB_PATH = DATA_DIR / "feed.db"

# Default window the outcome-watcher passes. User-tuneable via Settings later
# if needed -- 4h covers most intraday setups without trapping overnight runs.
DEFAULT_WINDOW_S = 4 * 3600


State = Literal["hit", "no_entry", "pending"]
Method = Literal["tick", "bar"]


class EntryResult(TypedDict):
    state:     State
    hit_ts:    int | None    # unix milliseconds (None unless state=='hit')
    method:    Method | None


def resolve_entry(
    instrument: str,
    entry_ts: int,
    entry: float,
    *,
    max_window_s: int = DEFAULT_WINDOW_S,
    tick_size: float | None = None,
    now_ts: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> EntryResult:
    """Was the entry price touched at-or-after entry_ts within max_window_s?

    Args:
        instrument:    stripped form ('MES', 'CL'), matches HelmFeed's publish.
        entry_ts:      unix seconds the signal was generated.
        entry:         entry price emitted by the LLM.
        max_window_s:  seconds after entry_ts before we declare no_entry.
        tick_size:     half this is the tick-level tolerance for an exact
                       match; if None we use 0.001 (subdollar tolerant).
        now_ts:        unix seconds; defaults to time.time(). Lets tests
                       pin the clock.
        conn:          pre-opened feed.db ro connection (mostly for tests).
    """
    if now_ts is None:
        now_ts = int(time.time())
    expired = (now_ts - entry_ts) >= max_window_s
    tol = (tick_size / 2.0) if tick_size and tick_size > 0 else 0.001

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(f"file:{FEED_DB_PATH}?mode=ro", uri=True)

    try:
        period = outcome_resolver._pick_finest_period(conn, instrument, entry_ts)
        if period is None:
            # No data covers the window yet. Pending unless expired.
            return {"state": "no_entry" if expired else "pending",
                    "hit_ts": None, "method": None}

        # First bar at-or-after entry_ts where bar.l <= entry <= bar.h.
        crossing = _find_first_touching_bar(conn, instrument, period, entry_ts, entry)
        if crossing is None:
            return {"state": "no_entry" if expired else "pending",
                    "hit_ts": None, "method": None}

        bar_ts = crossing
        period_secs = outcome_resolver.PERIOD_SECS[period]
        ts_ms_hi = bar_ts * 1000
        ts_ms_lo = max((bar_ts - period_secs) * 1000, entry_ts * 1000)

        tick_hit = _first_tick_touching_entry(
            conn, instrument, ts_ms_lo, ts_ms_hi, entry, tol)
        if tick_hit is not None:
            return {"state": "hit", "hit_ts": tick_hit, "method": "tick"}

        # No ticks in the bar's window -- use bar.ts as best-available timestamp.
        return {"state": "hit", "hit_ts": bar_ts * 1000, "method": "bar"}

    finally:
        if own_conn:
            conn.close()


def _find_first_touching_bar(
    conn: sqlite3.Connection, instrument: str, period: str,
    entry_ts: int, entry: float,
) -> int | None:
    rows = conn.execute(
        "SELECT ts, h, l FROM bars "
        "WHERE instrument = ? AND period = ? AND ts >= ? "
        "ORDER BY ts ASC",
        (instrument, period, entry_ts),
    )
    for ts, h, l in rows:
        if l <= entry <= h:
            return ts
    return None


def _first_tick_touching_entry(
    conn: sqlite3.Connection, instrument: str,
    ts_ms_lo: int, ts_ms_hi: int, entry: float, tol: float,
) -> int | None:
    """First tick whose price is within tol of entry. Returns ts_ms or None."""
    row = conn.execute(
        "SELECT ts_ms FROM ticks "
        "WHERE instrument = ? AND ts_ms >= ? AND ts_ms <= ? "
        "AND price BETWEEN ? AND ? "
        "ORDER BY ts_ms ASC LIMIT 1",
        (instrument, ts_ms_lo, ts_ms_hi, entry - tol, entry + tol),
    ).fetchone()
    return row[0] if row else None
