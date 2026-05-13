"""Resolve a trade's outcome by walking bars + ticks in feed.db.

Two-stage:
  1. Bar pre-filter — find the first bar at-or-after entry_ts whose [low, high]
     range crosses target or stop. Picks the finest period available for the
     instrument (1m if present, else 5m, else …) so the bar-level pre-filter
     is as tight as the data allows.
  2. Tick walk — within that bar's time window, walk ticks in order. First
     tick that touches target → outcome=target. First tick that touches stop
     → outcome=stop. If two ticks at the *same* ts_ms touch both, stop wins
     (conservative tie-break — matches how a real broker would fill in a
     flash event).

If the crossing bar exists but no ticks were stored for its window, the
resolver falls back to bar-level resolution. If both target and stop are
inside that bar's range and we have no ticks, it returns 'stop' for the same
conservative reason.

Touched semantics throughout: long target hit when price ≥ target; long
stop hit when price ≤ stop. Mirror for short.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal, TypedDict

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FEED_DB_PATH = DATA_DIR / "feed.db"

# Period label → seconds. Order of PERIOD_PREFERENCE picks the finest
# available period for stage 1's bar pre-filter.
PERIOD_SECS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400,
    "1d": 86400, "1w": 604800,
}
PERIOD_PREFERENCE: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")


Direction = Literal["long", "short"]
OutcomeKind = Literal["target", "stop", "neither"]
Method = Literal["tick", "bar"]


class Outcome(TypedDict):
    outcome: OutcomeKind
    hit_ts: int | None      # unix milliseconds when the level was touched (None if neither)
    hit_price: float | None
    method: Method | None   # which stage produced the answer


def resolve_outcome(
    instrument: str,
    direction: Direction,
    entry_ts: int,
    target: float,
    stop: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> Outcome:
    """Walk feed.db forward from entry_ts; return first target/stop hit, or 'neither'.

    Args:
        instrument:  stripped form, e.g. 'MES' (matches what HelmFeed publishes).
        direction:   'long' or 'short'.
        entry_ts:    unix seconds; resolution starts at-or-after this timestamp.
        target, stop: prices.
        conn:        optional sqlite3 connection (mostly for tests). Defaults to
                     opening feed.db in read-only mode.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(f"file:{FEED_DB_PATH}?mode=ro", uri=True)

    try:
        period = _pick_finest_period(conn, instrument, entry_ts)
        if period is None:
            return _outcome_neither()

        # Stage 1: first bar at-or-after entry_ts whose range crosses a level.
        crossing = _find_crossing_bar(conn, instrument, period, entry_ts,
                                      direction, target, stop)
        if crossing is None:
            return _outcome_neither()

        bar_ts, target_in_bar, stop_in_bar = crossing

        # Stage 2: walk ticks inside the crossing bar's window.
        period_secs = PERIOD_SECS[period]
        ts_ms_hi = bar_ts * 1000
        ts_ms_lo = max((bar_ts - period_secs) * 1000, entry_ts * 1000)

        tick_result = _resolve_in_ticks(conn, instrument, ts_ms_lo, ts_ms_hi,
                                        direction, target, stop)
        if tick_result is not None:
            return tick_result

        # No ticks in the window — fall back to bar-level resolution. Bar's
        # close ts is the best timestamp we have; bar's range crossed exactly
        # the level we report.
        hit_ts_ms = bar_ts * 1000
        if target_in_bar and stop_in_bar:
            # Ambiguous, no ticks → conservative tie-break to stop.
            return Outcome(outcome="stop", hit_ts=hit_ts_ms, hit_price=stop, method="bar")
        if target_in_bar:
            return Outcome(outcome="target", hit_ts=hit_ts_ms, hit_price=target, method="bar")
        return Outcome(outcome="stop", hit_ts=hit_ts_ms, hit_price=stop, method="bar")

    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _outcome_neither() -> Outcome:
    return Outcome(outcome="neither", hit_ts=None, hit_price=None, method=None)


def _pick_finest_period(conn: sqlite3.Connection, instrument: str,
                        entry_ts: int) -> str | None:
    """Pick the finest period that has at least one bar AT-OR-AFTER entry_ts.

    Originally this just took the finest period present in feed.db for the
    instrument. That broke for instruments where a finer period (e.g. 1m)
    had been published briefly and then stopped -- the resolver would pick
    1m for a signal generated days later, find no bars in the forward
    window, and return 'neither' forever. Filtering by entry_ts ensures
    we pick the finest period whose data actually covers the trade."""
    for p in PERIOD_PREFERENCE:
        row = conn.execute(
            "SELECT 1 FROM bars WHERE instrument = ? AND period = ? "
            "AND ts >= ? LIMIT 1",
            (instrument, p, entry_ts),
        ).fetchone()
        if row:
            return p
    return None


def _find_crossing_bar(
    conn: sqlite3.Connection,
    instrument: str,
    period: str,
    entry_ts: int,
    direction: Direction,
    target: float,
    stop: float,
) -> tuple[int, bool, bool] | None:
    """First bar at-or-after entry_ts where range crosses target or stop.
    Returns (close_ts, target_in_bar, stop_in_bar) or None."""
    rows = conn.execute(
        "SELECT ts, h, l FROM bars "
        "WHERE instrument = ? AND period = ? AND ts >= ? "
        "ORDER BY ts ASC",
        (instrument, period, entry_ts),
    )
    for ts, h, l in rows:
        target_in, stop_in = _bar_crosses(direction, h, l, target, stop)
        if target_in or stop_in:
            return ts, target_in, stop_in
    return None


def _bar_crosses(
    direction: Direction, bar_high: float, bar_low: float,
    target: float, stop: float,
) -> tuple[bool, bool]:
    if direction == "long":
        return bar_high >= target, bar_low <= stop
    return bar_low <= target, bar_high >= stop


def _price_touches(
    direction: Direction, price: float, target: float, stop: float,
) -> tuple[bool, bool]:
    if direction == "long":
        return price >= target, price <= stop
    return price <= target, price >= stop


def _resolve_in_ticks(
    conn: sqlite3.Connection, instrument: str,
    ts_ms_lo: int, ts_ms_hi: int,
    direction: Direction, target: float, stop: float,
) -> Outcome | None:
    """Walk ticks in [lo, hi]. Within a single ts_ms group, prefer stop hits
    over target hits — that's the conservative tie-break for the rare case
    where two prints land at the same millisecond."""
    rows = list(conn.execute(
        "SELECT ts_ms, price FROM ticks "
        "WHERE instrument = ? AND ts_ms >= ? AND ts_ms <= ? "
        "ORDER BY ts_ms ASC",
        (instrument, ts_ms_lo, ts_ms_hi),
    ))

    i = 0
    while i < len(rows):
        ts_ms = rows[i][0]
        # Collect every tick at this same ms.
        group_start = i
        while i < len(rows) and rows[i][0] == ts_ms:
            i += 1
        group = rows[group_start:i]

        # Stop wins ties — check it first across the group.
        for ts, price in group:
            _, stop_hit = _price_touches(direction, price, target, stop)
            if stop_hit:
                return Outcome(outcome="stop", hit_ts=ts, hit_price=price, method="tick")
        for ts, price in group:
            target_hit, _ = _price_touches(direction, price, target, stop)
            if target_hit:
                return Outcome(outcome="target", hit_ts=ts, hit_price=price, method="tick")
    return None
