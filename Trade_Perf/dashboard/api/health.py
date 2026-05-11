"""Health page endpoints — bot inference stats + unified log tail."""
from __future__ import annotations

import logging
import statistics
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import _tradebot_bridge as bridge  # noqa: F401  -- side-effect: sys.path
from src import signal_storage  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)


def compute_bot_health(sample: int = 50) -> dict[str, Any]:
    """Latency stats from the last N signals' duration_s field."""
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    signals = sorted(
        (rec for rec in raw.values() if not rec.get("deleted")),
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )
    durations = [s.get("duration_s") for s in signals[:sample]
                 if isinstance(s.get("duration_s"), (int, float))]
    out: dict[str, Any] = {
        "model": signals[0].get("model") if signals else None,
        "sample_size": len(durations),
        "latency_p50_s": None,
        "latency_p95_s": None,
        "latency_min_s": None,
        "latency_max_s": None,
    }
    if durations:
        sorted_d = sorted(durations)
        idx_95 = max(0, int(0.95 * (len(sorted_d) - 1)))
        out["latency_p50_s"] = round(statistics.median(sorted_d), 2)
        out["latency_p95_s"] = round(sorted_d[idx_95], 2)
        out["latency_min_s"] = round(sorted_d[0], 2)
        out["latency_max_s"] = round(sorted_d[-1], 2)
    return out


@router.get("/bot-stats")
def bot_stats() -> dict[str, Any]:
    return compute_bot_health()


@router.get("/logs")
def logs(lines: int = Query(300, ge=1, le=5000)) -> dict[str, Any]:
    """Tail the last N lines of TradingBot's tradebot.log (unified bot + API feed).

    The log file is written by both TradingBot's pipeline (main.py / pipeline.py /
    signal_storage.py) and the FastAPI app (configured in main.py at startup).
    """
    log_path = bridge.SIGNALS_LOG.parent / "tradebot.log"
    if not log_path.is_file():
        return {"path": str(log_path), "total_lines": 0, "lines": []}
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        raise HTTPException(500, f"could not read log: {e}") from e
    tail = all_lines[-lines:] if lines < len(all_lines) else all_lines
    return {
        "path": str(log_path),
        "total_lines": len(all_lines),
        "lines": [line.rstrip("\n") for line in tail],
    }
