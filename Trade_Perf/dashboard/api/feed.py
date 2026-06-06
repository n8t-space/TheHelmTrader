"""Live market-feed ingest from the NinjaScript publisher.

Endpoints:

  POST /api/feed/bar    — single bar close (low rate, ~1/min/chart)
  POST /api/feed/ticks  — batch of ticks (high rate, batched ~250ms by NS)
  POST /api/feed/prune  — manually trigger retention prune; returns counts

Writes land in TradingBot/app/data/feed.db via src.feed_store, which dedupes
on PK conflict (bars upsert, ticks ignore-on-dupe). Sync DB calls are wrapped
in asyncio.to_thread so they don't block the event loop.

Bar handler also runs:
  - The session-gap warmup gate (skip analysis on the first bar after a
    >30 min silence, since that bar straddles the chaotic CME open).
  - The arming predicate over the auto_analysis_config table; armed bars
    fan out to the auto_analyzer worker queue with coalescing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge
from . import settings as settings_mod
from src import auto_analyzer, feed_store, signal_storage  # type: ignore[import-not-found]  # via bridge

# Latest-screenshot store for the headless / auto analyzer. One PNG per
# (instrument, period); each fresh bar overwrites the previous in place.
AUTO_SHOTS_DIR = bridge.SCREENSHOTS_DIR  # same dir snip screenshots land in
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _auto_screenshot_path(instrument: str, period: str) -> Path:
    safe_i = _SAFE_NAME.sub("_", instrument)
    safe_p = _SAFE_NAME.sub("_", period)
    return AUTO_SHOTS_DIR / f"auto_{safe_i}_{safe_p}.png"


def _save_auto_screenshot(instrument: str, period: str, b64: str) -> Path:
    AUTO_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _auto_screenshot_path(instrument, period)
    path.write_bytes(base64.b64decode(b64))
    return path


def _auto_context_path(instrument: str, period: str) -> Path:
    safe_i = _SAFE_NAME.sub("_", instrument)
    safe_p = _SAFE_NAME.sub("_", period)
    return AUTO_SHOTS_DIR / f"context_{safe_i}_{safe_p}.json"


def _save_auto_context(instrument: str, period: str, ctx: dict, bar_ts: int) -> Path:
    """Persist the NS-emitted rich context for the latest bar of this combo,
    mirroring the screenshot cache. The headless analyzer reads it and verifies
    the embedded bar_ts so it never reasons over a different bar's context."""
    AUTO_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _auto_context_path(instrument, period)
    payload = {"bar_ts": bar_ts, "context": ctx}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path

router = APIRouter(prefix="/api/feed", tags=["feed"])
logger = logging.getLogger(__name__)

feed_store.init_schema()

# Session-gap warmup: a >30 min silence on an instrument means CME maintenance
# / weekend / holiday close. The next bar is "first of session" and we skip
# the analysis trigger for it (data is still stored). After-restart behavior:
# the first bar per instrument is always treated as post-gap, which is fine —
# we'd rather skip one bar than fire on stale state.
GAP_SECONDS        = 30 * 60   # silence > this = session boundary, skip first bar after
STALE_BAR_SECONDS  = 120       # bar.ts older than this vs now = backfill, store but don't analyze
_last_bar_ts: dict[str, int] = {}
# Newest bar ts we've already DISPATCHED analysis for, per (instrument, period).
# A re-sent / duplicate bar (HelmFeed double-posts the same close) is stored but
# must NOT trigger a second analysis -> a second signal -> a second order.
_last_dispatch_ts: dict[tuple[str, str], int] = {}


class Bar(BaseModel):
    instrument: str = Field(min_length=1, max_length=16)
    period: str = Field(min_length=1, max_length=8)
    ts: int = Field(ge=0)
    o: float
    h: float
    l: float
    c: float
    v: int = Field(ge=0)
    # HelmFeed attaches a chart bitmap on live bar close so the headless
    # analyzer can see the chart, not just bar text. Absent on historical
    # backfill (BackfillHistoricalBars publishes JSON without it).
    screenshot_b64: str | None = None
    # Rich NS market context (EMA/ADXR/Donchian/pivots/market-structure),
    # emitted by the merged HelmFeed on primary bar close. Absent on the old
    # HelmFeed and on backfill -> headless falls back to its thin context.
    context: dict | None = None


class Tick(BaseModel):
    ts_ms: int = Field(ge=0)
    price: float
    volume: int = Field(ge=0)


class TickBatch(BaseModel):
    instrument: str = Field(min_length=1, max_length=16)
    ticks: list[Tick]


@router.post("/bar")
async def ingest_bar(bar: Bar) -> dict:
    last_ts = _last_bar_ts.get(bar.instrument)
    if last_ts is None:
        # In-memory state was reset (e.g. a dashboard restart). Don't mistake
        # that for a >30 min session gap: seed from the last bar already in
        # feed.db. If bars were flowing (recent stored bar), this is just a
        # restart and the first live bar still arms -- only a genuine gap in the
        # STORED data triggers warmup. (One-time per instrument; later bars hit
        # the in-memory cache and skip this lookup.)
        last_ts = await asyncio.to_thread(feed_store.last_bar_ts, bar.instrument, bar.period)
    is_post_gap = (last_ts is None) or (bar.ts - last_ts) > GAP_SECONDS
    if last_ts is None or bar.ts > last_ts:
        _last_bar_ts[bar.instrument] = bar.ts

    # Stale-bar detection: a bar whose close ts is materially behind wall-
    # clock now is a historical backfill (HelmFeed publishes the last ~100
    # bars on indicator apply to warm feed.db without making the user wait
    # ~100 min for live bars to accumulate). Store but don't trigger
    # analysis — we only want analysis on fresh, currently-closing bars.
    import time
    is_stale = (int(time.time()) - bar.ts) > STALE_BAR_SECONDS

    await asyncio.to_thread(
        feed_store.insert_bar,
        bar.instrument, bar.period, bar.ts,
        bar.o, bar.h, bar.l, bar.c, bar.v,
    )

    if is_post_gap:
        return {"status": "ok", "armed": False, "reason": "post-gap warmup"}
    if is_stale:
        return {"status": "ok", "armed": False, "reason": "stale (backfill)"}

    # Persist the latest screenshot per (instrument, period) so the headless
    # analyzer can hand it to the vision LLM. One file per combo, overwritten
    # in place -- storage stays bounded at ~200 KB * armed_combos. Only when
    # the bar is fresh + post-warmup (NOT a backfill).
    if bar.screenshot_b64:
        try:
            _save_auto_screenshot(bar.instrument, bar.period, bar.screenshot_b64)
        except Exception:
            logger.exception("failed to save auto screenshot for %s @ %s",
                             bar.instrument, bar.period)

    # Persist the NS rich context (same combo cache) BEFORE dispatch so the
    # headless analyzer finds this bar's context when it runs. Old HelmFeed
    # sends no context -> headless falls back to its thin context.
    if bar.context:
        try:
            _save_auto_context(bar.instrument, bar.period, bar.context, bar.ts)
        except Exception:
            logger.exception("failed to save auto context for %s @ %s",
                             bar.instrument, bar.period)

    armed = await asyncio.to_thread(feed_store.is_armed, bar.instrument, bar.period)
    if armed:
        # Automation blackout: pause signal generation during a configured window.
        blackout, label = settings_mod.in_blackout()
        if blackout:
            return {"status": "ok", "armed": False, "reason": f"automation blackout ({label})"}
        # Dispatch dedup (one analysis per bar): only fire for a bar NEWER than the
        # last we analyzed for this instrument+period. A re-sent/duplicate bar_ts
        # is stored above but never re-analyzed -> no duplicate signal/order. The
        # check + claim are sync (no await between) so concurrent dupes can't race.
        key = (bar.instrument, bar.period)
        if bar.ts <= _last_dispatch_ts.get(key, 0):
            return {"status": "ok", "armed": False, "reason": "duplicate bar (already analyzed)"}
        _last_dispatch_ts[key] = bar.ts
        await auto_analyzer.submit(bar.instrument, bar.period, bar.ts)

    return {"status": "ok", "armed": armed}


@router.post("/ticks")
async def ingest_ticks(batch: TickBatch) -> dict:
    rows = [
        (batch.instrument, t.ts_ms, t.price, t.volume)
        for t in batch.ticks
    ]
    await asyncio.to_thread(feed_store.insert_ticks, rows)
    return {"status": "ok", "count": len(rows)}


@router.post("/prune")
async def prune_feed(retention_days: int = 7) -> dict:
    """Delete bars and ticks older than the retention cutoff.

    Cutoff is min(now − retention_days, oldest unresolved trade entry) so an
    open trade can't lose the data the outcome resolver needs to walk."""
    protected_ts = await asyncio.to_thread(_oldest_open_trade_ts)
    result = await asyncio.to_thread(
        feed_store.prune, retention_days, protected_ts,
    )
    result["protected_ts_seconds"] = protected_ts
    return result


def _oldest_open_trade_ts() -> int | None:
    """Earliest entry time (unix s) of any unresolved, non-deleted, directional
    signal in signals.jsonl. None if no open trades."""
    try:
        signals = signal_storage.load_all(bridge.SIGNALS_LOG)
    except FileNotFoundError:
        return None

    oldest: int | None = None
    for ts_iso, rec in signals.items():
        if rec.get("deleted"):
            continue
        outcome = rec.get("outcome") or {}
        if outcome.get("result"):
            continue  # resolved
        proposal = rec.get("proposal") or {}
        if proposal.get("direction") == "flat":
            continue
        try:
            ts_s = int(datetime.fromisoformat(ts_iso).timestamp())
        except (ValueError, TypeError):
            continue
        if oldest is None or ts_s < oldest:
            oldest = ts_s
    return oldest
