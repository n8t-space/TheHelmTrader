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

# Open upper bound for tick walks (year ~5138 in ms). Used by the tick-first
# fallback so we can scan every tick at-or-after entry without a bar window.
_TS_MS_MAX = 99_999_999_999_999


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
        # Tick-first: ticks are the actual prints and the most precise + current
        # data. Walk them directly whenever any exist at-or-after entry. This is
        # the path that survives stale/absent bars (HelmFeed publishing ticks but
        # not bars) -- the bar-anchored stages below would otherwise miss the hit.
        if _has_ticks_after(conn, instrument, entry_ts):
            return (_resolve_in_ticks(conn, instrument, entry_ts * 1000, _TS_MS_MAX,
                                      direction, target, stop)
                    or _outcome_neither())

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


# ---------------------------------------------------------------------------
# Per-bracket simulator (scale-out ATMs)
# ---------------------------------------------------------------------------
#
# Each bracket is a mini state machine: starts at initial stop, may step to
# break-even when MFE crosses auto_be_trigger, may begin trailing when MFE
# crosses a trail step's profit_trigger, never moves backward. Resolves at
# the first event (tick or bar) where price touches target or the current stop.
#
# All input tick distances are in ticks (NT8 native unit); prices are converted
# from ticks-from-entry to absolute price using entry + sign * ticks * tick_size.
# The walker processes a single chronologically-sorted event stream and
# advances every still-open bracket against each event, so legs are
# automatically consistent with the underlying tape.

class BracketSpec(TypedDict, total=False):
    qty:              int
    stop_ticks:       int                       # initial stop distance, ticks from entry (positive)
    target_ticks:     int                       # target distance, ticks from entry (positive)
    auto_be_plus:     int                       # BE offset above entry once armed (ticks)
    auto_be_trigger:  int                       # MFE at which BE arms (ticks)
    trail_steps:      list[dict[str, int]]      # [{frequency, profit_trigger, stop_loss}]


class Leg(TypedDict):
    bracket_idx: int
    qty:         int
    result:      str             # "target" | "stop" | "trail" | "be" | "neither"
    exit_price:  float | None
    exit_ts:     int | None      # unix ms (matches tick.ts_ms / bar.ts*1000)
    method:      str | None      # "tick" | "bar"


def resolve_brackets(
    instrument: str,
    direction: Direction,
    entry_ts: int,
    entry_price: float,
    tick_size: float,
    brackets: list[dict],
    *,
    conn: sqlite3.Connection | None = None,
) -> list[Leg]:
    """Run the per-bracket state machine over feed.db tape from entry_ts.

    Returns one Leg per input bracket in the same order. Open brackets at the
    end of available data return result='neither' with exit_price/exit_ts/method
    all None -- caller can leave them in 'pending' or apply the bar-level fallback.

    Empty bracket list -> returns []. Empty tape -> all legs 'neither'.
    """
    if not brackets:
        return []
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")

    sign = 1 if direction == "long" else -1
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(f"file:{FEED_DB_PATH}?mode=ro", uri=True)

    # Per-bracket runtime state. current_stop_ticks is the signed distance from
    # entry in ticks (e.g. -8 means 8 ticks below entry for a long; once armed
    # to BE+1 it becomes +1, etc.). Distances are sign-flipped for shorts.
    state: list[dict] = []
    for spec in brackets:
        state.append({
            "spec":              spec,
            "open":              True,
            "mfe_ticks":         0.0,                # ticks of favorable move from entry
            "current_stop_ticks": -float(spec.get("stop_ticks") or 0),  # negative = below entry for long
            "be_armed":          False,
            "trail_armed":       False,
            "active_trail_idx":  -1,                 # which trail step is currently driving
            "last_trail_anchor": None,               # MFE ticks at last trail-step bump
            "leg":               Leg(bracket_idx=0, qty=int(spec.get("qty") or 0),
                                     result="neither", exit_price=None,
                                     exit_ts=None, method=None),
        })
        state[-1]["leg"]["bracket_idx"] = len(state) - 1

    try:
        # Stream events tick-first: if any ticks exist at-or-after entry, walk
        # them ALL directly (most precise, and survives stale/absent bars). Only
        # if there are no ticks do we fall back to the bar-anchored tape. The
        # walker processes one event at a time so trail/BE state advances in
        # chronological order across all brackets.
        if _has_ticks_after(conn, instrument, entry_ts):
            events = _stream_ticks_only(conn, instrument, entry_ts)
        else:
            period = _pick_finest_period(conn, instrument, entry_ts)
            events = _stream_tape(conn, instrument, period, entry_ts) if period else iter([])

        for ev in events:
            if all(not s["open"] for s in state):
                break
            ts_ms, low_price, high_price, method = ev
            for s in state:
                if not s["open"]:
                    continue
                # Within a single event we need to consider the worst of the
                # two prices first (stop side), then the best (target side) --
                # conservative tie-break matching _resolve_in_ticks.
                _step_bracket(s, sign, entry_price, tick_size,
                              low_price, high_price, ts_ms, method)

        # Anything still open at end-of-tape stays 'neither' (caller decides
        # whether to leave it pending or flag).
        return [s["leg"] for s in state]
    finally:
        if own_conn:
            conn.close()


def _step_bracket(s: dict, sign: int, entry_price: float, tick_size: float,
                  low_price: float, high_price: float, ts_ms: int,
                  method: str) -> None:
    """Advance one bracket's state machine by a single tick (or bar).

    For ticks low_price == high_price. For bars they're the (low, high) of
    the bar -- we then probe stop with the unfavorable extreme and target
    with the favorable extreme.

    Order within an event matters:
      1. Update MFE based on favorable extreme
      2. Arm BE if MFE crossed auto_be_trigger
      3. Arm / advance trail if MFE crossed a trail step's profit_trigger
      4. Check stop (current_stop) against unfavorable extreme — STOP WINS TIES
      5. Check target against favorable extreme
    """
    spec = s["spec"]
    fav_price = high_price if sign > 0 else low_price
    unfav_price = low_price if sign > 0 else high_price

    # ticks of favorable movement from entry (always >= 0; negative means not yet positive)
    fav_ticks = (fav_price - entry_price) / tick_size * sign
    if fav_ticks > s["mfe_ticks"]:
        s["mfe_ticks"] = fav_ticks

    # --- Step 2: auto-BE arm ----------------------------------------------
    be_trig = spec.get("auto_be_trigger") or 0
    if not s["be_armed"] and be_trig > 0 and s["mfe_ticks"] >= be_trig:
        be_plus = spec.get("auto_be_plus") or 0
        new_stop_ticks = float(be_plus)  # positive: above entry for long, below for short
        if new_stop_ticks > s["current_stop_ticks"]:
            s["current_stop_ticks"] = new_stop_ticks
        s["be_armed"] = True

    # --- Step 3: trail step arm / advance ---------------------------------
    trail_steps: list = spec.get("trail_steps") or []
    if trail_steps:
        # Pick highest-trigger step whose threshold has been crossed -- handles
        # NT8 multi-step staircase trails. Single-step ATMs degrade trivially.
        candidate_idx = -1
        for i, step in enumerate(trail_steps):
            if s["mfe_ticks"] >= (step.get("profit_trigger") or 0):
                candidate_idx = i
        if candidate_idx >= 0:
            step = trail_steps[candidate_idx]
            freq = step.get("frequency") or 0
            sl_dist = step.get("stop_loss") or 0
            if not s["trail_armed"] or candidate_idx != s["active_trail_idx"]:
                # Activation: set stop to MFE - stop_loss (in ticks-from-entry).
                proposed = s["mfe_ticks"] - sl_dist
                if proposed > s["current_stop_ticks"]:
                    s["current_stop_ticks"] = proposed
                s["trail_armed"]       = True
                s["active_trail_idx"]  = candidate_idx
                s["last_trail_anchor"] = s["mfe_ticks"]
            else:
                # Already trailing -- only step when MFE has advanced by freq ticks
                # since the last anchor (freq=0 means continuous tracking).
                anchor = s["last_trail_anchor"] or s["mfe_ticks"]
                if freq <= 0 or (s["mfe_ticks"] - anchor) >= freq:
                    proposed = s["mfe_ticks"] - sl_dist
                    if proposed > s["current_stop_ticks"]:
                        s["current_stop_ticks"] = proposed
                    s["last_trail_anchor"] = s["mfe_ticks"]

    # --- Step 4: stop check (wins ties) -----------------------------------
    # current_stop in absolute price terms:
    stop_price = entry_price + sign * s["current_stop_ticks"] * tick_size
    if (sign > 0 and unfav_price <= stop_price) or (sign < 0 and unfav_price >= stop_price):
        # Classify: was it the initial stop, BE, or a trailing stop?
        if s["trail_armed"]:
            result = "trail"
        elif s["be_armed"]:
            result = "be"
        else:
            result = "stop"
        s["leg"].update(open=False, result=result, exit_price=stop_price,
                        exit_ts=ts_ms, method=method)
        s["open"] = False
        return

    # --- Step 5: target check ---------------------------------------------
    target_ticks = spec.get("target_ticks") or 0
    if target_ticks > 0:
        target_price = entry_price + sign * target_ticks * tick_size
        if (sign > 0 and fav_price >= target_price) or (sign < 0 and fav_price <= target_price):
            s["leg"].update(open=False, result="target", exit_price=target_price,
                            exit_ts=ts_ms, method=method)
            s["open"] = False


def _has_ticks_after(conn: sqlite3.Connection, instrument: str, entry_ts: int) -> bool:
    """True if any tick exists at-or-after entry_ts for the instrument."""
    return conn.execute(
        "SELECT 1 FROM ticks WHERE instrument = ? AND ts_ms >= ? LIMIT 1",
        (instrument, entry_ts * 1000),
    ).fetchone() is not None


def _stream_ticks_only(conn: sqlite3.Connection, instrument: str, entry_ts: int):
    """Yield (ts_ms, price, price, 'tick') for every tick at-or-after entry_ts.

    Bar-independent: used when ticks are present so a stop/target/trail is caught
    even when feed.db has no (or stale) bars covering the trade. Ticks are the
    real prints, so this is also more precise than the bar-anchored walk."""
    rows = conn.execute(
        "SELECT ts_ms, price FROM ticks "
        "WHERE instrument = ? AND ts_ms >= ? ORDER BY ts_ms ASC",
        (instrument, entry_ts * 1000),
    )
    for ts_ms, price in rows:
        yield (ts_ms, float(price), float(price), "tick")


def _stream_tape(conn: sqlite3.Connection, instrument: str, period: str,
                 entry_ts: int):
    """Yield (ts_ms, low_price, high_price, method) events in chronological order.

    Prefers ticks (high==low==price, method='tick'). Within a bar window with
    no ticks, falls back to a single bar-event using (l, h, method='bar').
    The order is: bars provide the time skeleton, ticks fill in within each
    bar's window.
    """
    period_secs = PERIOD_SECS[period]
    bars = list(conn.execute(
        "SELECT ts, h, l FROM bars "
        "WHERE instrument = ? AND period = ? AND ts >= ? "
        "ORDER BY ts ASC",
        (instrument, period, entry_ts),
    ))
    for bar_ts, h, l in bars:
        ts_ms_hi = bar_ts * 1000
        ts_ms_lo = max((bar_ts - period_secs) * 1000, entry_ts * 1000)
        ticks = list(conn.execute(
            "SELECT ts_ms, price FROM ticks "
            "WHERE instrument = ? AND ts_ms >= ? AND ts_ms <= ? "
            "ORDER BY ts_ms ASC",
            (instrument, ts_ms_lo, ts_ms_hi),
        ))
        if ticks:
            for ts_ms, price in ticks:
                yield (ts_ms, float(price), float(price), "tick")
        else:
            # Bar fallback: emit ONE event using the bar's high+low so the
            # step function can probe both stop and target sides at once.
            yield (ts_ms_hi, float(l), float(h), "bar")


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
