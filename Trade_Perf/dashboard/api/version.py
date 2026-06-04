"""Update-check + one-click apply endpoints.

`/api/version` (GET)  -> cached HEAD-vs-origin/main comparison.
`/api/version/check` (POST) -> force a fresh fetch + compare.
`/api/version/update` (POST) -> kick off the in-place updater (detached
PowerShell helper that git-pulls, rebuilds, kills uvicorn -- watchdog
respawns with new code).
`/api/version/update/status` (GET) -> read the helper's progress JSON.

Runs `git fetch` in a background task on a slow cadence (default 6h) so the
foreground request path is always cheap. Falls back to safe `{available:false}`
on any error so the banner just stays hidden instead of throwing in the UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/version", tags=["version"])
logger = logging.getLogger(__name__)

# dashboard/api/version.py -> dashboard/api -> dashboard -> Trade_Perf -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

CHECK_INTERVAL_S = 6 * 60 * 60      # 6h between background fetches
GIT_TIMEOUT_S    = 30
DEFAULT_BRANCH   = "main"
DEFAULT_REMOTE   = "origin"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "current_sha":      None,
    "current_short":    None,
    "latest_sha":       None,
    "latest_short":     None,
    "commits_behind":   0,
    "update_available": False,
    "last_checked":     None,   # unix seconds; None == never run
    "last_error":       None,
    "is_git_checkout":  None,   # None until first check
    "remote":           DEFAULT_REMOTE,
    "branch":           DEFAULT_BRANCH,
}


def _run_git(args: list[str], timeout: int = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run `git <args>` in REPO_ROOT. Returns (returncode, stdout, stderr).
    Never raises -- a missing git binary or a hung fetch returns (-1, '', err)."""
    cmd = ["git", "-C", str(REPO_ROOT), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return -1, "", "git binary not found on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 -- subprocess can surface odd OS errors
        return -1, "", f"git {' '.join(args)} failed: {e}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _is_git_checkout() -> bool:
    if not (REPO_ROOT / ".git").exists():
        return False
    rc, _, _ = _run_git(["rev-parse", "--is-inside-work-tree"])
    return rc == 0


def _compute_once() -> dict[str, Any]:
    """One-shot: fetch + compare. Mutates module state under the lock and
    returns a snapshot of the new state."""
    with _lock:
        now = time.time()
        if not _is_git_checkout():
            _state.update({
                "is_git_checkout":  False,
                "update_available": False,
                "last_checked":     now,
                "last_error":       "not a git checkout (release-zip install)",
            })
            return dict(_state)
        _state["is_git_checkout"] = True

        # Capture HEAD up-front -- needed even if the fetch fails.
        rc, head, err = _run_git(["rev-parse", "HEAD"])
        if rc != 0 or not head:
            _state.update({
                "update_available": False,
                "last_checked":     now,
                "last_error":       err or "could not read HEAD",
            })
            return dict(_state)
        _state["current_sha"]   = head
        _state["current_short"] = head[:7]

        # Refresh remote refs. Quiet mode; surface stderr only if it failed.
        rc, _, err = _run_git(["fetch", "--quiet", DEFAULT_REMOTE, DEFAULT_BRANCH])
        if rc != 0:
            _state.update({
                "last_checked": now,
                "last_error":   f"fetch failed: {err or 'unknown'}",
            })
            # Don't reset update_available -- keep last known state, just
            # mark the check as errored so the UI can flag stale data.
            return dict(_state)

        rc, latest, err = _run_git(
            ["rev-parse", f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"]
        )
        if rc != 0 or not latest:
            _state.update({
                "last_checked": now,
                "last_error":   err or "could not resolve remote ref",
            })
            return dict(_state)
        _state["latest_sha"]   = latest
        _state["latest_short"] = latest[:7]

        if latest == head:
            _state.update({
                "commits_behind":   0,
                "update_available": False,
                "last_checked":     now,
                "last_error":       None,
            })
            return dict(_state)

        # Count how far HEAD is behind the remote tip.
        rc, count, _ = _run_git(
            ["rev-list", "--count", f"HEAD..{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"]
        )
        behind = int(count) if (rc == 0 and count.isdigit()) else 0
        _state.update({
            "commits_behind":   behind,
            "update_available": behind > 0,
            "last_checked":     now,
            "last_error":       None,
        })
        return dict(_state)


async def check_loop_forever() -> None:
    """Background task — runs one check on startup, then every CHECK_INTERVAL_S."""
    while True:
        try:
            snap = await asyncio.to_thread(_compute_once)
            if snap.get("update_available"):
                logger.info(
                    "[version] update available: %s -> %s (%d commits behind)",
                    snap.get("current_short"), snap.get("latest_short"),
                    snap.get("commits_behind"),
                )
            elif snap.get("last_error"):
                logger.warning("[version] check errored: %s", snap["last_error"])
            else:
                logger.info(
                    "[version] up to date at %s", snap.get("current_short")
                )
        except Exception:
            logger.exception("[version] check loop iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_S)


@router.get("")
def get_version() -> dict[str, Any]:
    """Return the cached version-check state.

    Cheap: never blocks on git. The background task refreshes it every 6h, and
    `POST /api/version/check` forces a refresh on demand."""
    with _lock:
        return dict(_state)


@router.post("/check")
async def force_check() -> dict[str, Any]:
    """Force a fresh fetch + compare. Used by the 'Check now' button."""
    snap = await asyncio.to_thread(_compute_once)
    return snap


# ---------------------------------------------------------------------------
# One-click in-place updater
# ---------------------------------------------------------------------------

# Status file: persists across the uvicorn restart triggered at the end of
# the update, so the frontend's poll resumes cleanly against the new instance.
UPDATE_STATUS_PATH = Path.home() / ".helm" / "update-status.json"
UPDATE_SCRIPT_PATH = REPO_ROOT / "Trade_Perf" / "runtime" / "update.ps1"


def _read_status() -> dict[str, Any]:
    if not UPDATE_STATUS_PATH.is_file():
        return {"stage": "idle"}
    try:
        # utf-8-sig: PS 5.1's Set-Content/Out-File write a UTF-8 BOM that
        # plain "utf-8" would surface as a leading ﻿ and break json.loads.
        return json.loads(UPDATE_STATUS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return {"stage": "unknown", "error": f"could not read status file: {e}"}


def _spawn_update_helper() -> int:
    """Copy update.ps1 to %TEMP% (so a git reset on the source file mid-run
    can't break the running helper), launch it detached, return the PID."""
    if not UPDATE_SCRIPT_PATH.is_file():
        raise FileNotFoundError(f"update script missing at {UPDATE_SCRIPT_PATH}")

    tmp_dir   = Path(tempfile.gettempdir()) / "helm-update"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_copy  = tmp_dir / f"update-{int(time.time())}.ps1"
    shutil.copy2(UPDATE_SCRIPT_PATH, tmp_copy)

    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        raise RuntimeError("neither pwsh nor powershell.exe is on PATH")

    cmd = [
        pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(tmp_copy),
        "-UvicornPid", str(os.getpid()),
        "-RepoRoot", str(REPO_ROOT),
        "-StatusPath", str(UPDATE_STATUS_PATH),
    ]
    # DETACHED_PROCESS = 0x00000008 | CREATE_NEW_PROCESS_GROUP = 0x00000200.
    # Together with close_fds=True this guarantees the helper survives the
    # uvicorn kill it will perform at the end.
    creationflags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return proc.pid


@router.post("/update")
def start_update() -> dict[str, Any]:
    """Kick off the one-click updater. Returns 202-style ack with the helper
    PID; the frontend then polls /api/version/update/status until stage=done."""
    snap = get_version()
    if not snap.get("is_git_checkout"):
        raise HTTPException(409, "not a git checkout; cannot self-update")
    if not snap.get("update_available"):
        raise HTTPException(409, "already up to date; nothing to apply")

    # If a previous helper is mid-run, refuse to spawn a second one.
    status = _read_status()
    stage  = status.get("stage")
    if stage in ("fetching", "pip", "npm", "build"):
        raise HTTPException(409, f"update already in progress (stage={stage})")

    try:
        pid = _spawn_update_helper()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(500, str(e)) from e

    # Seed the status file immediately so the frontend's first poll sees
    # 'queued' instead of stale data from a prior run.
    try:
        UPDATE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_STATUS_PATH.write_text(
            json.dumps({
                "stage": "queued",
                "message": "Update helper spawned",
                "step": 0,
                "total_steps": 6,
                "log_tail": [],
                "started_at": None,
                "finished_at": None,
                "error": None,
                "target_sha": snap.get("latest_sha"),
                "pid": pid,
            }),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("[version] could not seed update status file: %s", e)

    logger.info("[version] update helper spawned (pid=%s)", pid)
    return {"started": True, "pid": pid, "status_path": str(UPDATE_STATUS_PATH)}


@router.get("/update/status")
def update_status() -> dict[str, Any]:
    """Poll target. Returns the helper's last-written progress snapshot, or
    {stage: 'idle'} if no update has ever been attempted on this install."""
    return _read_status()


# ---------------------------------------------------------------------------
# Restart endpoint -- kick uvicorn without re-deploying code
# ---------------------------------------------------------------------------

# Refuse to restart while these update stages are in flight -- the updater
# is going to restart uvicorn at its own tail step, so racing it is pointless
# (best case) or destroys mid-flight state (worst case).
_UPDATE_BUSY_STAGES = {"queued", "fetching", "pip", "npm", "build"}


@router.post("/restart")
def restart_uvicorn() -> dict[str, Any]:
    """Restart uvicorn so it reloads on-disk code, without re-deploying. The
    dashboard's 'Restart Helm' button on Support/Overview hits this.

    The process exits ITSELF (after the response flushes); the watchdog, which
    owns this uvicorn as a child, sees the port go quiet within its 5s poll and
    spawns a fresh one against the current code. We self-exit rather than
    Stop-Process an external PID: this uvicorn runs in Session 0 as the NSSM
    service account, so an out-of-process Stop-Process against it is refused
    with 'Access is denied' -- but a process can always terminate itself."""
    status = _read_status()
    stage  = status.get("stage")
    if stage in _UPDATE_BUSY_STAGES:
        raise HTTPException(
            409,
            f"update in progress (stage={stage}); restart blocked. Wait for the update to finish.",
        )

    pid = os.getpid()

    def _self_terminate() -> None:
        # Brief grace so the HTTP response reaches the client before we die.
        # os._exit (not sys.exit) guarantees the interpreter goes down now --
        # equivalent to the -Force kill the watchdog would have done.
        time.sleep(0.75)
        os._exit(0)

    threading.Thread(target=_self_terminate, name="helm-restart", daemon=True).start()
    logger.info("[version] restart: uvicorn pid=%s self-exiting; watchdog respawns", pid)
    return {"restarting": True, "pid": pid, "eta_seconds": 7}
