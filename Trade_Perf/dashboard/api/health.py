"""Health page endpoints — bot inference stats + unified log tail."""
from __future__ import annotations

import logging
import statistics
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import _tradebot_bridge as bridge  # noqa: F401  -- side-effect: sys.path
from . import settings as settings_mod
from src import signal_storage  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)


def _configured_model(ai) -> str:
    """Pick the model field that matches the active provider, so the Bot
    Health card reflects what the NEXT inference call will actually use --
    not the model the LAST signal happened to run on."""
    if ai.provider == "ollama":   return ai.model
    if ai.provider == "claude":   return ai.claude_model
    if ai.provider == "openai":   return ai.openai_model
    return ""


def compute_bot_health(sample: int = 50) -> dict[str, Any]:
    """Live AI config + latency stats from the last N signals' duration_s.

    The provider / configured_model fields reflect what's in Settings RIGHT
    NOW (so a Settings change shows up instantly on the Health card). The
    last_used_model field shows what the last actually-captured signal
    ran on, which lags by one inference call after a config swap and is
    surfaced as a divergence hint when the two don't match."""
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    signals = sorted(
        (rec for rec in raw.values() if not rec.get("deleted")),
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )
    durations = [s.get("duration_s") for s in signals[:sample]
                 if isinstance(s.get("duration_s"), (int, float))]

    ai = settings_mod.get_settings().ai_backend
    configured = _configured_model(ai)
    last_used  = signals[0].get("model") if signals else None

    # Per-provider extras the UI surfaces.
    ollama_url       = ai.ollama_url    if ai.provider == "ollama" else None
    fallback_model   = ai.fallback_model if ai.provider == "ollama" else None
    api_key_configured = (
        (ai.provider == "claude" and bool(ai.claude_api_key)) or
        (ai.provider == "openai" and bool(ai.openai_api_key))
    )

    out: dict[str, Any] = {
        # Live config (Settings is source of truth):
        "provider":               ai.provider,
        "configured_model":       configured,
        "configured_fallback":    fallback_model,
        "ollama_url":             ollama_url,
        "api_key_configured":     api_key_configured,
        "request_timeout_s":      ai.request_timeout_s,
        # Latest signal's model -- diverges from configured after a swap:
        "last_used_model":        last_used,
        # Back-compat: keep `model` so older SPA builds still render.
        "model":                  last_used,
        "sample_size":            len(durations),
        "latency_p50_s":          None,
        "latency_p95_s":          None,
        "latency_min_s":          None,
        "latency_max_s":          None,
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


def _check_ai_reachable() -> dict[str, Any]:
    """Reachability of the inference backend used for SIGNALS. For a local Ollama
    we actually GET /api/tags (this is the box that physically drops off the
    network); for a hosted provider we report whether the API key is configured
    (a network probe would cost a call and can false-fail behind a corp TLS
    proxy)."""
    import urllib.request

    ai = settings_mod.get_settings().ai_backend
    provider = ai.signal_provider or ai.provider
    if provider == "ollama":
        base = ai.ollama_url.rsplit("/api/", 1)[0] or "http://127.0.0.1:11434"
        try:
            req = urllib.request.Request(base + "/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=4) as r:
                ok = 200 <= r.status < 300
            detail = base if ok else f"{base} -> HTTP {r.status}"
        except Exception as e:  # noqa: BLE001 -- any failure = unreachable
            ok = False
            detail = f"{base} unreachable ({type(e).__name__})"
        return {"name": "AI inference (Ollama)", "key": "ai",
                "reachable": ok, "critical": True, "detail": detail}

    key_ok = ((provider == "claude" and bool(ai.claude_api_key))
              or (provider == "openai" and bool(ai.openai_api_key)))
    return {"name": f"AI inference ({provider})", "key": "ai",
            "reachable": key_ok, "critical": True,
            "detail": "API key set" if key_ok else "no API key configured"}


def _check_feed() -> dict[str, Any]:
    """Freshness of the NinjaTrader bar feed. Non-critical (stale is normal when
    the market is closed) -- shown for awareness, never force-pops the alert."""
    try:
        import sqlite3
        import time
        from src import feed_store  # type: ignore[import-not-found]
        conn = sqlite3.connect(f"file:{feed_store.DB_PATH}?mode=ro", uri=True, timeout=4)
        try:
            (latest,) = conn.execute("SELECT MAX(ts) FROM bars").fetchone()
        finally:
            conn.close()
        age_s = (time.time() - latest) if latest else None
        ok = age_s is not None and age_s < 300
        detail = "no bars" if latest is None else f"last bar {int(age_s)}s ago"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"feed check failed ({type(e).__name__})"
    return {"name": "NinjaTrader feed", "key": "feed",
            "reachable": ok, "critical": False, "detail": detail}


@router.get("/services")
def services() -> dict[str, Any]:
    """Per-service reachability for the dashboard's service-down alert. The SPA
    polls this and pops a modal when any `critical` service is unreachable."""
    svcs = [_check_ai_reachable(), _check_feed()]
    return {"services": svcs,
            "any_critical_down": any(s["critical"] and not s["reachable"] for s in svcs)}


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
