"""Tests for src.auto_analyzer — coalescing queue + worker behavior."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def reset_analyzer():
    """Wipe module-level state between tests."""
    from src import auto_analyzer
    yield auto_analyzer
    # teardown
    auto_analyzer._pending.clear()
    if auto_analyzer._worker_task is not None:
        auto_analyzer._worker_task.cancel()
    auto_analyzer._worker_task = None
    auto_analyzer._pending_lock = None
    auto_analyzer._wake_event = None


@pytest.mark.asyncio
async def test_submit_starts_worker_lazily(reset_analyzer):
    aa = reset_analyzer
    assert aa._worker_task is None
    await aa.submit("MES", "5m", 1000)
    assert aa._worker_task is not None
    # Let it drain
    await asyncio.sleep(0.05)
    assert aa.queue_size() == 0


@pytest.mark.asyncio
async def test_run_count_increments(reset_analyzer):
    aa = reset_analyzer
    pre = aa.stats()["run_count"]
    await aa.submit("MES", "5m", 1000)
    await asyncio.sleep(0.05)
    post = aa.stats()["run_count"]
    assert post == pre + 1


@pytest.mark.asyncio
async def test_coalescing_collapses_same_key(reset_analyzer, monkeypatch):
    """Slow stub forces queue to fill; verify coalescing reduces runs."""
    aa = reset_analyzer
    seen = []

    async def slow_run(job):
        seen.append(job)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(aa, "_run_analysis", slow_run)

    # 6 submits across 3 keys, fast — should collapse
    await aa.submit("MES", "5m", 100)
    await aa.submit("MES", "5m", 200)   # replaces 100
    await aa.submit("MES", "5m", 300)   # replaces 200
    await aa.submit("MCL", "5m", 100)
    await aa.submit("MCL", "5m", 200)   # replaces 100
    await aa.submit("NQ",  "15m", 50)

    # Drain
    for _ in range(20):
        await asyncio.sleep(0.05)
        if aa.queue_size() == 0:
            break
    await asyncio.sleep(0.1)

    assert len(seen) <= 6, "should not exceed total submits"
    assert len(seen) < 6, f"coalescing should reduce runs (<6); got {len(seen)}"

    # Whatever ran, the freshest bar_ts per key must win
    latest = {(j["instrument"], j["period"]): j["bar_ts"] for j in seen}
    assert latest.get(("MES", "5m"))  == 300
    assert latest.get(("MCL", "5m"))  == 200
    assert latest.get(("NQ",  "15m")) == 50


@pytest.mark.asyncio
async def test_stats_reports_queue_and_runs(reset_analyzer):
    aa = reset_analyzer
    s0 = aa.stats()
    assert s0["queue_size"] == 0
    assert s0["worker_alive"] is False  # never started

    await aa.submit("MES", "5m", 1)
    await asyncio.sleep(0.05)
    s1 = aa.stats()
    assert s1["worker_alive"] is True
    assert s1["queue_size"] == 0  # drained
    assert s1["last_run"] is not None
