"""FastAPI app exposing /api/* endpoints over the local trades.db.

Run from the project root:
    python -m uvicorn dashboard.api.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import _tradebot_bridge as bridge, atm_strategies as atm_routes, auto_analysis as auto_analysis_routes, db, drawdown as drawdown_routes, feed as feed_routes, health as health_routes, home as home_routes, settings as settings_routes, signals as signals_routes, trades as tradelib, version as version_routes

logger = logging.getLogger(__name__)


def _configure_unified_logging() -> None:
    """Send FastAPI-side logs into TradingBot's tradebot.log so the Health
    page can render a single consolidated feed (bot pipeline + dashboard).
    Idempotent across uvicorn --reload cycles."""
    log_path = bridge.SIGNALS_LOG.parent / "tradebot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    abs_path = str(log_path.resolve())
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_path:
            return  # already attached
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [api] %(message)s"
    ))
    fh.setLevel(logging.INFO)
    root.addHandler(fh)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


_configure_unified_logging()


# ---------------------------------------------------------------------------
# Background tasks: nightly feed.db prune
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager
import asyncio

# Start the prune ~10 min after the dashboard comes up (avoid hammering
# the disk during the trading-day startup rush) then run every 24 h.
# Retention window itself is user-configurable via Settings.
PRUNE_FIRST_DELAY_S = 600
PRUNE_INTERVAL_S    = 86_400


async def _prune_loop_forever() -> None:
    """Periodic prune task — runs in the FastAPI event loop."""
    from src import feed_store, signal_storage  # type: ignore[import-not-found]
    from . import _tradebot_bridge as bridge

    await asyncio.sleep(PRUNE_FIRST_DELAY_S)
    while True:
        try:
            # Compute "oldest unresolved trade entry" so the prune doesn't
            # wipe data the outcome resolver still needs.
            from datetime import datetime
            oldest = None
            try:
                signals = signal_storage.load_all(bridge.SIGNALS_LOG)
                for ts_iso, rec in signals.items():
                    if rec.get("deleted"): continue
                    outcome = rec.get("outcome") or {}
                    if outcome.get("result"): continue
                    proposal = rec.get("proposal") or {}
                    if proposal.get("direction") == "flat": continue
                    try:
                        ts_s = int(datetime.fromisoformat(ts_iso).timestamp())
                    except (ValueError, TypeError):
                        continue
                    if oldest is None or ts_s < oldest:
                        oldest = ts_s
            except FileNotFoundError:
                pass

            from . import settings as settings_mod
            retention = settings_mod.get_settings().strategy.retention_days
            result = await asyncio.to_thread(
                feed_store.prune, retention, oldest)
            logger.info("[auto-prune] %s", result)
        except Exception:
            logger.exception("[auto-prune] iteration failed")

        await asyncio.sleep(PRUNE_INTERVAL_S)


@asynccontextmanager
async def lifespan(app):
    """Spin up background tasks on startup, cancel on shutdown."""
    from src import outcome_watcher  # type: ignore[import-not-found]  # via bridge

    prune_task   = asyncio.create_task(_prune_loop_forever(),                       name="feed.prune")
    outcome_task = asyncio.create_task(outcome_watcher.watcher_loop(bridge.SIGNALS_LOG), name="outcome.watcher")
    version_task = asyncio.create_task(version_routes.check_loop_forever(),         name="version.check")
    logger.info("[startup] background tasks started: prune, outcome-watcher, version-check")
    try:
        yield
    finally:
        for t in (outcome_task, prune_task, version_task):
            t.cancel()
        for t in (outcome_task, prune_task, version_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[shutdown] background tasks stopped")


app = FastAPI(title="The Helm — API", version="0.1.0", lifespan=lifespan)

# Vite dev server runs on 5173. Allow it (and 5174 fallback) during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(signals_routes.router)
app.include_router(home_routes.router)
app.include_router(health_routes.router)
app.include_router(feed_routes.router)
app.include_router(auto_analysis_routes.router)
app.include_router(settings_routes.router)
app.include_router(atm_routes.router)
app.include_router(version_routes.router)
app.include_router(drawdown_routes.router)


@app.get("/api/health")
def health():
    try:
        with db.connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM fills").fetchone()
        return {"status": "ok", "fills": n, "db_path": str(db.DB_PATH)}
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


@app.get("/api/dimensions")
def dimensions():
    try:
        return db.list_dimensions()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


@app.get("/api/fills")
def fills(
    account: Annotated[list[str] | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    strategy: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query(description="ISO 8601 UTC, inclusive")] = None,
    date_to: Annotated[str | None, Query(description="ISO 8601 UTC, exclusive")] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    try:
        rows = db.fetch_fills(
            account=account, symbol=symbol, strategy=strategy,
            date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    return {"count": len(rows), "fills": rows}


@app.get("/api/trades")
def trades(
    account: Annotated[list[str] | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
):
    try:
        fills_rows = db.fetch_fills_for_derivation(
            account=account, symbol=symbol,
            date_from=date_from, date_to=date_to,
        )
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    trades_rows = tradelib.derive_trades(fills_rows)
    return {"count": len(trades_rows), "trades": trades_rows}


@app.get("/api/stats")
def stats(
    account: Annotated[list[str] | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
):
    try:
        fills_rows = db.fetch_fills_for_derivation(
            account=account, symbol=symbol,
            date_from=date_from, date_to=date_to,
        )
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    trades_rows = tradelib.derive_trades(fills_rows)
    return tradelib.compute_stats(trades_rows)


def _capture_with_context_async(ctx: dict, image_path: Path | None) -> None:
    """Background worker for /api/capture-from-nt.

    If `image_path` is provided (NS captured the chart bitmap directly), the
    pipeline uses it verbatim -- no Snipping overlay. This is the preferred
    path post-2026-05-12 since it bypasses the Session-0 URI-handler issue
    that breaks the snipping overlay when uvicorn is hosted as a service.

    If `image_path` is None (legacy NS that doesn't send a screenshot), the
    pipeline falls back to opening the Windows Snipping overlay via the
    `ms-screenclip:` URI -- fragile under Session-0 isolation.
    """
    from src.pipeline import run_pipeline  # type: ignore[import-not-found]
    try:
        prompt = bridge.PROMPT_FILE.read_text(encoding="utf-8")
        record = run_pipeline(bridge.SCREENSHOTS_DIR, bridge.SIGNALS_LOG, prompt,
                              market_context=ctx, image_path=image_path)
        logger.info("NT-triggered analysis complete: %s", record["timestamp"])
    except RuntimeError as e:
        logger.warning("NT-triggered capture aborted: %s", e)
    except Exception:
        logger.exception("NT-triggered pipeline failed")


def _decode_screenshot(b64: str, screenshots_dir: Path) -> Path:
    """Decode a base64-encoded PNG from the NS payload and persist it to the
    screenshots dir under the standard timestamp filename. Returns the path."""
    import base64
    from datetime import datetime
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = screenshots_dir / f"{stamp}.png"
    path.write_bytes(base64.b64decode(b64))
    return path


@app.post("/api/capture-from-nt", status_code=202)
def capture_from_nt(payload: dict):
    """NinjaScript bridge endpoint -- called by HelmAnalyzer on Ctrl+Shift+F.

    Payload shape:
        instrument:     str (required)
        timeframes:     dict (optional)
        daily_levels:   dict (optional)
        current:        dict (optional)
        screenshot_b64: str (optional)  -- if present, used directly;
                       absent path falls back to the Snipping overlay flow.

    Persists the market-context payload (minus screenshot_b64) for audit,
    decodes the screenshot if provided, then spawns a background pipeline.
    Returns 202 immediately so NS isn't held on the HTTP connection.
    """
    if not isinstance(payload, dict):
        logger.warning("NT POST rejected -- body did not parse as JSON dict.")
        raise HTTPException(400, "body did not parse as JSON object")
    if "instrument" not in payload:
        logger.warning("NT POST rejected -- no 'instrument' key. keys=%s",
                       list(payload.keys()))
        raise HTTPException(400, "expected an 'instrument' key")

    # Refuse the trigger up-front if no AI provider is configured. NS sees
    # a 503 + reason; user gets a clear "configure a provider in Settings"
    # message instead of a silent failure or a half-completed pipeline.
    from src import runtime_config  # type: ignore[import-not-found]  # via bridge
    ok, why = runtime_config.is_provider_configured()
    if not ok:
        logger.warning("NT trigger rejected -- AI provider not configured: %s", why)
        raise HTTPException(503, why)

    # Pull the screenshot out of the payload so market_context.json doesn't
    # bloat with a 200-400 KB base64 blob on every trigger.
    screenshot_b64 = payload.pop("screenshot_b64", None)
    image_path: Path | None = None
    if screenshot_b64:
        try:
            image_path = _decode_screenshot(screenshot_b64, bridge.SCREENSHOTS_DIR)
            logger.info("NT screenshot decoded: %s (%d bytes)",
                        image_path.name, image_path.stat().st_size)
        except Exception:
            logger.exception("NT screenshot decode failed -- falling back to snip overlay")
            image_path = None

    try:
        bridge.MARKET_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        bridge.MARKET_CONTEXT_PATH.write_text(json.dumps(payload, indent=2),
                                              encoding="utf-8")
    except OSError as e:
        logger.warning("Could not persist market_context.json: %s", e)

    src = "embedded screenshot" if image_path else "snip overlay (legacy NS)"
    logger.info("NT trigger received for %s via %s -- spawning background pipeline",
                payload.get("instrument"), src)
    threading.Thread(
        target=_capture_with_context_async,
        args=(payload, image_path),
        daemon=True,
    ).start()
    return {"status": "accepted", "instrument": payload.get("instrument"),
            "source": "embedded" if image_path else "snip"}


@app.get("/api/screenshots/{filename}")
def screenshot(filename: str):
    """Serve a captured chart screenshot from TradingBot's data/screenshots/."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "invalid filename")
    path = bridge.SCREENSHOTS_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "screenshot not found")
    return FileResponse(path)


# Serve the production-built frontend if a dist/ exists. This keeps the runtime
# to a single process when the watchdog launches us — no separate Vite dev
# server needed. During active frontend development, the user still runs
# `run_dev.ps1` which starts Vite at :5173 with hot-reload.
#
# IMPORTANT: this catch-all GET is registered LAST so /api/* and other API
# routes (defined above) take precedence. For unmatched paths, we fall back to
# index.html so React Router's client-side routes (/health, /signals/{ts})
# work on a hard refresh.
_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if not _WEB_DIST.is_dir():
    logger.warning("No frontend build at %s — only /api/* routes are reachable. "
                   "Run `npm run build` in dashboard/web/ to enable.", _WEB_DIST)


@app.get("/{full_path:path}")
def spa_or_static(full_path: str):
    """Catch-all for non-API GETs. Serves files from dist/ when present,
    otherwise falls back to index.html (SPA client-side routing)."""
    if not _WEB_DIST.is_dir():
        raise HTTPException(404, "frontend not built; run `npm run build` in dashboard/web/")
    target = _WEB_DIST / (full_path or "index.html")
    # Prevent path traversal
    try:
        target.resolve().relative_to(_WEB_DIST.resolve())
    except ValueError:
        raise HTTPException(403, "invalid path") from None
    if target.is_file():
        return FileResponse(target)
    return FileResponse(_WEB_DIST / "index.html")
