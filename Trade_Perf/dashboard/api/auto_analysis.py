"""Auto-analysis config + diagnostics.

GET  /api/auto-analysis/config  — current armed (instrument, period) list.
PUT  /api/auto-analysis/config  — replace the whole list (atomic).
GET  /api/auto-analysis/status  — queue size, run count, last run, worker liveness.

Dashboard uses GET/PUT on a 4-slot panel (full-list replace is simpler than
per-row CRUD). The 4-cap is enforced both UI-side and here as a sanity guard.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge  # noqa: F401  # ensures sys.path shim
from src import auto_analyzer, feed_store  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/auto-analysis", tags=["auto-analysis"])
logger = logging.getLogger(__name__)

MAX_ARMED = 4  # mirrors the dashboard's slot count


class Entry(BaseModel):
    instrument: str = Field(min_length=1, max_length=16)
    period:     str = Field(min_length=1, max_length=8)
    enabled:    bool


class ConfigPayload(BaseModel):
    entries: list[Entry]


@router.get("/config")
async def get_config() -> dict:
    rows = await asyncio.to_thread(feed_store.list_config)
    return {"entries": rows}


@router.put("/config")
async def put_config(payload: ConfigPayload) -> dict:
    armed = sum(1 for e in payload.entries if e.enabled)
    if armed > MAX_ARMED:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_ARMED} armed entries allowed; got {armed}.",
        )

    # Reject duplicate (instrument, period) keys — would silently collapse on
    # PRIMARY KEY conflict otherwise.
    seen: set[tuple[str, str]] = set()
    for e in payload.entries:
        key = (e.instrument, e.period)
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate (instrument, period): {key}",
            )
        seen.add(key)

    await asyncio.to_thread(
        feed_store.replace_config,
        [e.model_dump() for e in payload.entries],
    )
    rows = await asyncio.to_thread(feed_store.list_config)
    return {"entries": rows}


@router.get("/status")
async def get_status() -> dict:
    return auto_analyzer.stats()
