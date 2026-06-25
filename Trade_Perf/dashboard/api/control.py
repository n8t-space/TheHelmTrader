"""Helm kill switch.

A one-shot "stop the dashboard until NinjaTrader restarts" control. Writing the
sentinel signals the watchdog (runtime/watchdog.ps1) to take the dashboard down
and keep it down while the SAME NinjaTrader process instance that was running at
kill time stays alive. The watchdog clears it when NT restarts, AND on its own
startup -- so a manual Helm service restart also resumes the dashboard.

The API can't stop itself cleanly (it IS the uvicorn process); it just drops the
sentinel and returns. The watchdog kills uvicorn within one poll (~5 s).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from . import settings as settings_mod

router = APIRouter(prefix="/api/control", tags=["control"])
logger = logging.getLogger(__name__)

# Watchdog reads/stamps this file too. Keep it beside settings.json so both
# sides resolve the same ~/.helm dir.
KILL_SWITCH_PATH = settings_mod.HELM_HOME / "kill-switch.json"


@router.get("/kill")
def kill_status() -> dict[str, Any]:
    """Whether the kill switch is currently armed (sentinel present)."""
    if KILL_SWITCH_PATH.is_file():
        try:
            # utf-8-sig: the watchdog may rewrite this file (PS 5.1 can emit a
            # BOM); tolerate it so the status read never throws.
            data = json.loads(KILL_SWITCH_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
        return {"armed": True, **data}
    return {"armed": False}


@router.post("/kill")
def kill() -> dict[str, Any]:
    """Arm the kill switch. The watchdog stops the dashboard within ~5 s and
    keeps it down until NinjaTrader restarts or the Helm service is restarted."""
    settings_mod.HELM_HOME.mkdir(parents=True, exist_ok=True)
    payload = {"requested_at": datetime.now().isoformat(timespec="seconds")}
    KILL_SWITCH_PATH.write_text(json.dumps(payload), encoding="utf-8")
    logger.warning("[control] kill switch armed -- watchdog will stop the "
                   "dashboard until NinjaTrader (or the Helm service) restarts")
    return {
        "armed": True,
        "message": ("Helm will stop within ~5 s and stay down until "
                    "NinjaTrader restarts or the Helm service is restarted."),
        **payload,
    }


@router.delete("/kill")
def resume() -> dict[str, Any]:
    """Clear the kill switch (resume now), if the API is still reachable."""
    try:
        KILL_SWITCH_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    logger.info("[control] kill switch cleared via API")
    return {"armed": False}
