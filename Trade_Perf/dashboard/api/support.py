"""Support-bundle endpoint — package the logs + sanitized diagnostics into a
streamed zip the user can download and email to the maintainer.

Privacy:
- API keys are stripped from the settings snapshot.
- Trade data (trades.db), signals.jsonl, screenshots, and feed.db are
  excluded -- those are large + sensitive and not generally needed for a
  bug report. Operator can attach them manually if specifically asked.
- Account IDs ARE included so the maintainer can correlate with the trade
  history the operator has on hand. They're operator-internal IDs, not
  credit-card-grade secrets.
"""
from __future__ import annotations

import io
import json
import logging
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from . import _tradebot_bridge as bridge  # noqa: F401  -- side-effect: sys.path
from . import db, settings as settings_mod, version as version_mod
from src import signal_storage  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api/support", tags=["support"])

logger = logging.getLogger(__name__)

# Logs to include in the bundle (path + display name).
LOG_PATHS: list[tuple[Path, str]] = [
    (bridge.SIGNALS_LOG.parent / "tradebot.log",   "tradebot.log"),
    (Path(__file__).resolve().parents[2] / "data" / "watchdog.log",     "watchdog.log"),
    (Path(__file__).resolve().parents[2] / "data" / "service.out.log",  "service.out.log"),
    (Path(__file__).resolve().parents[2] / "data" / "service.err.log",  "service.err.log"),
]

# Tail size per log file. 2 MB is enough for several hours of normal activity;
# truncates from the FRONT so the most recent events survive.
MAX_LOG_BYTES = 2 * 1024 * 1024


def _tail_bytes(path: Path, max_bytes: int) -> bytes:
    if not path.is_file():
        return b""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            # Skip partial first line so the bundle never starts mid-record.
            f.readline()
        return f.read()


def _sanitized_settings() -> dict[str, Any]:
    """Settings snapshot with API keys redacted. Everything else is kept --
    appearance / strategy / accounts / drawdowns help reproduce the operator's
    setup."""
    s = settings_mod.get_settings()
    raw = s.model_dump()
    ai = raw.get("ai_backend") or {}
    for k in ("claude_api_key", "openai_api_key"):
        if ai.get(k):
            ai[k] = "*** REDACTED ***"
    return raw


def _diag_manifest() -> dict[str, Any]:
    """Best-effort environment snapshot. Stays best-effort: any individual
    probe failure logs + returns None rather than aborting the bundle."""
    version_state: dict[str, Any] = {}
    try:
        version_state = version_mod.get_version()  # type: ignore[arg-type]
    except Exception:
        try:
            with version_mod._lock:
                version_state = dict(version_mod._state)
        except Exception:
            version_state = {"error": "version state unavailable"}

    counts: dict[str, Any] = {}
    try:
        with db.connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM fills").fetchone()
            counts["fills_in_trades_db"] = n
    except Exception as e:
        counts["fills_in_trades_db_error"] = str(e)
    try:
        sigs = signal_storage.load_all(bridge.SIGNALS_LOG)
        counts["signals_total"] = len(sigs)
        counts["signals_visible"] = sum(1 for s in sigs.values() if not s.get("deleted"))
    except Exception as e:
        counts["signals_error"] = str(e)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "system":      platform.system(),
            "release":     platform.release(),
            "version":     platform.version(),
            "machine":     platform.machine(),
            "python":      sys.version.split()[0],
        },
        "version": version_state,
        "counts":  counts,
        "settings": _sanitized_settings(),
    }


def _bundle_readme(manifest: dict[str, Any]) -> str:
    short = (manifest.get("version") or {}).get("current_short") or "?"
    when  = manifest.get("generated_at_utc", "?")
    return (
        "# The Helm — Support Bundle\n\n"
        f"Generated: {when}\n"
        f"Installed commit: `{short}`\n\n"
        "## Contents\n\n"
        "- `manifest.json` -- environment snapshot, settings (API keys redacted), "
        "and record counts for fills + signals.\n"
        "- `tradebot.log` -- unified bot + dashboard log (last 2 MB).\n"
        "- `watchdog.log` -- NSSM service lifecycle (start/stop, uvicorn spawn).\n"
        "- `service.out.log` / `service.err.log` -- raw stdout/stderr captured by NSSM.\n\n"
        "## Excluded by design\n\n"
        "The bundle deliberately leaves out:\n\n"
        "- `trades.db` (fill history -- attach manually if the maintainer asks)\n"
        "- `signals.jsonl` (LLM proposals, may contain prompts you'd rather not share)\n"
        "- `feed.db` (live bars + ticks)\n"
        "- `data/screenshots/*` (captured chart images)\n\n"
        "## Privacy notes\n\n"
        "- API keys in settings are redacted with `*** REDACTED ***`.\n"
        "- NT account IDs are NOT redacted -- the maintainer needs them to "
        "correlate with whatever trade list you sent alongside this bundle.\n"
        "- Logs may contain account IDs, instrument symbols, and timestamps. "
        "Review `tradebot.log` and `manifest.json` before emailing if you want "
        "to scrub anything further.\n"
    )


def _build_zip() -> bytes:
    buf = io.BytesIO()
    manifest = _diag_manifest()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", _bundle_readme(manifest))
        z.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        for path, name in LOG_PATHS:
            data = _tail_bytes(path, MAX_LOG_BYTES)
            if data:
                z.writestr(name, data)
            else:
                z.writestr(name, f"(file not found or empty at {path})\n".encode("utf-8"))
    return buf.getvalue()


@router.get("/log-bundle")
def log_bundle() -> StreamingResponse:
    """Return a streamed zip the user can save + attach to a support email.

    Filename includes the installed commit short SHA so the maintainer can
    immediately tell what version produced the report.
    """
    try:
        manifest = _diag_manifest()
    except Exception:
        manifest = {}
    short = (manifest.get("version") or {}).get("current_short") or "unknown"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"helm-support-{stamp}-{short}.zip"

    data = _build_zip()
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control":       "no-store",
    }
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers=headers,
    )
