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
import logging
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge
from src import auto_analyzer, feed_store, signal_storage  # type: ignore[import-not-found]  # via bridge

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


class Bar(BaseModel):
    instrument: str = Field(min_length=1, max_length=16)
    period: str = Field(min_length=1, max_length=8)
    ts: int = Field(ge=0)
    o: float
    h: float
    l: float
    c: float
    v: int = Field(ge=0)


class Tick(BaseModel):
    ts_ms: int = Field(ge=0)
    price: float
    volume: int = Field(ge=0)


class TickBatch(BaseModel):
    instrument: str = Field(min_length=1, max_length=16)
    ticks: list[Tick]


@router.post("/bar")
async def ingest_bar(bar: Bar) -> dict:
    # Sync block — no awaits between the read and write of _last_bar_ts so
    # concurrent bar arrivals for the same instrument don't race.
    last_ts = _last_bar_ts.get(bar.instrument)
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

    armed = await asyncio.to_thread(feed_store.is_armed, bar.instrument, bar.period)
    if armed:
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
