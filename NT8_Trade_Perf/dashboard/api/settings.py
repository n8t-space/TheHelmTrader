"""User-editable runtime settings.

Stored per-user at ``~/.helm/settings.json`` (Windows: ``C:\\Users\\<u>\\.helm\\``).
Loaded once at process start; PUT /api/settings hot-reloads the in-memory copy.
Modules consume via ``get_settings()`` -- no per-request file I/O.

Backwards compatibility: every field has a default that matches the prior
hardcoded behavior, so a missing settings file produces an unchanged dashboard.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path.home() / ".helm" / "settings.json"
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class Appearance(BaseModel):
    theme: str = Field(default="dark", pattern="^(dark|light|system)$")
    # CSS color tokens. Validated as 6- or 8-digit hex.
    accent: str = Field(default="#58a6ff", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    bg: str = Field(default="#0e1116", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    panel: str = Field(default="#161b22", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    border: str = Field(default="#30363d", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    text: str = Field(default="#e6edf3", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    muted: str = Field(default="#7d8590", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    pos: str = Field(default="#3fb950", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    neg: str = Field(default="#f85149", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    timezone: str = Field(default="America/Chicago")
    table_page_size: int = Field(default=100, ge=10, le=2000)


class AiBackend(BaseModel):
    ollama_url: str = Field(default="http://<workstation-LAN-IP>:11434/api/generate")
    model: str = Field(default="qwen2.5vl:7b")
    fallback_model: str = Field(default="minicpm-v:latest")
    request_timeout_s: int = Field(default=300, ge=10, le=1800)
    num_ctx: int = Field(default=8192, ge=2048, le=131072)


class Strategy(BaseModel):
    confidence_floor: float = Field(default=0.75, ge=0.0, le=1.0)
    reconciliation_cap: int = Field(default=3, ge=0, le=20)
    max_attempts: int = Field(default=2, ge=1, le=5)
    retention_days: int = Field(default=7, ge=1, le=90)
    stale_bar_seconds: int = Field(default=120, ge=10, le=3600)


class Accounts(BaseModel):
    live: list[str] = Field(default_factory=lambda: ["<live-account-id>"])
    evals: list[str] = Field(default_factory=lambda: ["<eval-account-id>"])
    simulation: list[str] = Field(default_factory=lambda: [
        "<demo-account-id>", "SimBetaSIM", "Sim101", "Playback101", "Backtest",
    ])


class Settings(BaseModel):
    schema_version: int = SCHEMA_VERSION
    appearance: Appearance = Field(default_factory=Appearance)
    ai_backend: AiBackend = Field(default_factory=AiBackend)
    strategy: Strategy = Field(default_factory=Strategy)
    accounts: Accounts = Field(default_factory=Accounts)


# ---------------------------------------------------------------------------
# Load / save / cache
# ---------------------------------------------------------------------------

_cache: Settings | None = None
_cache_lock = Lock()


def _load_from_disk() -> Settings:
    if not SETTINGS_PATH.is_file():
        logger.info("[settings] no file at %s; using defaults", SETTINGS_PATH)
        return Settings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[settings] failed to read %s (%s); using defaults", SETTINGS_PATH, e)
        return Settings()
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        logger.warning("[settings] invalid file at %s (%s); using defaults", SETTINGS_PATH, e)
        return Settings()


def get_settings() -> Settings:
    """Return the current in-memory settings. Cached for the process lifetime
    (invalidated on PUT). Cheap to call -- safe to use in hot paths."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = _load_from_disk()
    return _cache


def _save_to_disk(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.model_dump(), indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)  # atomic on Windows + POSIX


def _replace(settings: Settings) -> None:
    global _cache
    with _cache_lock:
        _save_to_disk(settings)
        _cache = settings


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings() -> dict[str, Any]:
    """Return current settings + schema metadata for the Settings page."""
    s = get_settings()
    return {
        "schema_version": SCHEMA_VERSION,
        "path": str(SETTINGS_PATH),
        "exists_on_disk": SETTINGS_PATH.is_file(),
        "settings": s.model_dump(),
    }


@router.put("")
def write_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist + hot-reload. Returns the canonical saved doc."""
    try:
        validated = Settings.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None
    _replace(validated)
    logger.info("[settings] saved to %s", SETTINGS_PATH)
    return {"settings": validated.model_dump(), "path": str(SETTINGS_PATH)}


@router.post("/reset")
def reset_settings() -> dict[str, Any]:
    """Reset to defaults. Does NOT delete the file; writes the default doc."""
    defaults = Settings()
    _replace(defaults)
    logger.info("[settings] reset to defaults")
    return {"settings": defaults.model_dump(), "path": str(SETTINGS_PATH)}


@router.post("/test/ollama")
def test_ollama() -> dict[str, Any]:
    """Ping the configured Ollama URL with a tiny request. Returns latency +
    available models, or an error string. UI surfaces this in a green/red badge."""
    import requests
    s = get_settings()
    url = s.ai_backend.ollama_url
    # Convert /api/generate -> /api/tags for a cheap GET
    if url.endswith("/api/generate"):
        probe = url[: -len("/api/generate")] + "/api/tags"
    else:
        probe = url
    t0 = time.monotonic()
    try:
        r = requests.get(probe, timeout=5)
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": str(e), "probed": probe}
    dt = time.monotonic() - t0
    try:
        tags = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        tags = []
    configured = s.ai_backend.model
    return {
        "ok": True,
        "probed": probe,
        "latency_s": round(dt, 3),
        "models": tags,
        "configured_model_present": configured in tags,
        "configured_model": configured,
    }
