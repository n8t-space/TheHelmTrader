"""Update-check endpoint — compares the installed checkout's HEAD against
origin/main and tells the frontend whether a newer commit is available.

Runs `git fetch` in a background task on a slow cadence (default 6h) so the
foreground request path is always cheap. Falls back to safe `{available:false}`
on any error so the banner just stays hidden instead of throwing in the UI.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

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
