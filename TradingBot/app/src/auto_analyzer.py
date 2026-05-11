"""Auto-analysis worker: coalescing asyncio queue + worker task.

Bar arrivals on /api/feed/bar drive this. The dashboard's auto-analysis
config table decides which (instrument, period) keys are armed; armed bars
get enqueued via ``submit()``. The worker drains in batches, deduplicating
on (instrument, period) — if a newer bar arrives for the same key while the
prior job is still queued, the older one is discarded (we always analyze
the freshest bar, never a stale one).

The actual analyzer call is a **stub** in v1: it logs the job. Replace
``_run_analysis`` once we've decided what auto-analysis output looks like
(text-only LLM with synthesized HTF context? cheap heuristic? something
else?). The infrastructure here doesn't change when we do.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Coalescing queue keyed on (instrument, period). New jobs replace older ones
# for the same key — the worker always handles the freshest bar.
_pending: dict[tuple[str, str], dict[str, Any]] = {}
_pending_lock: asyncio.Lock | None = None
_wake_event: asyncio.Event | None = None
_worker_task: asyncio.Task[None] | None = None

# Diagnostics — useful for the dashboard / health page.
_last_run: dict[str, Any] | None = None
_run_count: int = 0


def _ensure_started() -> None:
    """Lazy-init the lock/event/worker on first submit. Module-level
    asyncio primitives can't be created before an event loop exists, so we
    defer until we're in a running loop."""
    global _pending_lock, _wake_event, _worker_task
    if _pending_lock is None:
        _pending_lock = asyncio.Lock()
        _wake_event = asyncio.Event()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker(), name="auto_analyzer.worker")
        logger.info("auto-analyzer worker started")


async def submit(instrument: str, period: str, bar_ts: int) -> None:
    """Enqueue (or replace) the pending job for (instrument, period)."""
    _ensure_started()
    assert _pending_lock is not None and _wake_event is not None  # by _ensure_started
    async with _pending_lock:
        _pending[(instrument, period)] = {
            "instrument": instrument,
            "period":     period,
            "bar_ts":     bar_ts,
        }
        _wake_event.set()


def queue_size() -> int:
    """Best-effort count without acquiring the lock — for diagnostics."""
    return len(_pending)


def stats() -> dict[str, Any]:
    return {
        "queue_size": queue_size(),
        "run_count":  _run_count,
        "last_run":   _last_run,
        "worker_alive": (
            _worker_task is not None and not _worker_task.done()
        ),
    }


async def _worker() -> None:
    assert _pending_lock is not None and _wake_event is not None
    while True:
        await _wake_event.wait()

        # Drain coalesced jobs in one batch.
        async with _pending_lock:
            jobs = list(_pending.values())
            _pending.clear()
            _wake_event.clear()

        for job in jobs:
            try:
                await _run_analysis(job)
            except Exception:
                logger.exception("auto-analysis job failed: %s", job)


async def _run_analysis(job: dict[str, Any]) -> None:
    """Hand the job to headless_analyzer.analyze, off the event loop.

    The analyzer reads feed.db, calls workstation Ollama with text-only
    context, and persists the proposal via signal_storage. We run it on
    a thread because requests.post is blocking; keeping it off the
    event loop lets the queue keep coalescing fresher bars while a
    slow LLM call is in flight.
    """
    global _last_run, _run_count
    logger.info(
        "[auto-analysis] dispatching: %s @ %s (bar_ts=%s)",
        job["instrument"], job["period"], job["bar_ts"],
    )

    # Local import — avoids a hard requirement on the analyzer module
    # for callers (e.g., tests) that monkeypatch _run_analysis.
    from . import headless_analyzer

    record = await asyncio.to_thread(
        headless_analyzer.analyze,
        job["instrument"], job["period"], job["bar_ts"],
    )
    _run_count += 1
    _last_run = {
        **job,
        "ran_at_run_count": _run_count,
        "result_timestamp": record["timestamp"] if record else None,
        "skipped":          record is None,
    }
