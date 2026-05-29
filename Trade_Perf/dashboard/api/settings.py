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
    # Provider selector. Picks which vendor's vision API the analyzer hits.
    provider: str = Field(default="ollama", pattern="^(ollama|claude|openai)$")
    # Shared
    request_timeout_s: int = Field(default=300, ge=10, le=1800)

    # Ollama (local / LAN). Defaults to localhost; point at a LAN host
    # via the Settings page if you've offloaded inference to a workstation.
    ollama_url:     str = Field(default="http://127.0.0.1:11434/api/generate")
    model:          str = Field(default="qwen2.5vl:7b")     # the Ollama model
    fallback_model: str = Field(default="minicpm-v:latest")
    num_ctx:        int = Field(default=8192, ge=2048, le=131072)

    # Anthropic Claude (cloud). Vision via the Messages API.
    claude_api_key:    str = Field(default="")
    claude_model:      str = Field(default="claude-sonnet-4-6")
    claude_max_tokens: int = Field(default=2048, ge=256, le=16384)

    # OpenAI ChatGPT / GPT-4o (cloud). Vision via Chat Completions.
    openai_api_key:    str = Field(default="")
    openai_model:      str = Field(default="gpt-4o")
    openai_max_tokens: int = Field(default=2048, ge=256, le=16384)


class Strategy(BaseModel):
    confidence_floor: float = Field(default=0.75, ge=0.0, le=1.0)
    reconciliation_cap: int = Field(default=3, ge=0, le=20)
    max_attempts: int = Field(default=2, ge=1, le=5)
    retention_days: int = Field(default=7, ge=1, le=90)
    stale_bar_seconds: int = Field(default=120, ge=10, le=3600)


class DrawdownConfig(BaseModel):
    # Per-account prop-firm drawdown limits. Keys here are NT account IDs that
    # appear in `evals` (or `live` for funded accounts that still carry a
    # trailing DD). All amounts in account-currency dollars.
    starting_balance: float = Field(default=50000.0, ge=0.0)
    trailing_drawdown: float = Field(default=2500.0, ge=0.0)
    daily_drawdown: float = Field(default=1500.0, ge=0.0)
    profit_target: float = Field(default=3000.0, ge=0.0)


class Accounts(BaseModel):
    # Bucket your NT account IDs so the Home page's cumulative-earnings card
    # aggregates correctly. Set these via the Settings page after first run.
    # NT's default sim accounts (Sim101, Playback101, Backtest, SimBetaSIM) are
    # pre-listed under 'simulation' because every NT install has them; the
    # other buckets start empty.
    live: list[str] = Field(default_factory=list)
    evals: list[str] = Field(default_factory=list)
    simulation: list[str] = Field(default_factory=lambda: [
        "Sim101", "Playback101", "Backtest", "SimBetaSIM",
    ])
    # Per-account drawdown configuration, keyed by NT account ID. Only the
    # accounts you want tracked (typically your prop-firm Evals) need entries
    # here. Missing entries get NO drawdown tracking -- the account just
    # doesn't show up in the Drawdown card.
    drawdowns: dict[str, DrawdownConfig] = Field(default_factory=dict)


class News(BaseModel):
    """Economic-calendar widget config. Two sources:

      forexfactory  -- the public XML feed at
                       https://nfs.faireconomy.media/ff_calendar_thisweek.xml.
                       No AI required; works offline-ish (one HTTP call).
      econoday      -- https://us.econoday.com/byweek scraped HTML, then
                       extracted by the configured AI provider. AI must be
                       reachable for this source to contribute.

    Filter rules apply to the merged event list before render. Empty
    impact_filter / currency_filter == no filter.
    """
    enabled: bool = True
    forexfactory_enabled: bool = True
    econoday_enabled: bool = True
    impact_filter: list[str] = Field(default_factory=lambda: ["High"])
    currency_filter: list[str] = Field(default_factory=lambda: ["USD"])
    refresh_interval_minutes: int = Field(default=15, ge=5, le=180)


class Settings(BaseModel):
    schema_version: int = SCHEMA_VERSION
    appearance: Appearance = Field(default_factory=Appearance)
    ai_backend: AiBackend = Field(default_factory=AiBackend)
    strategy: Strategy = Field(default_factory=Strategy)
    accounts: Accounts = Field(default_factory=Accounts)
    news: News = Field(default_factory=News)


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


def visible_accounts() -> set[str]:
    """Source-of-truth set for which NT account IDs are visible to the rest of
    the site. Union of the three Settings buckets (live + evals + simulation).
    Any account not in this set is hidden from /api/dimensions, FilterBar,
    Home cumulative-earnings, and default unfiltered fill queries -- the
    recorder keeps writing fills for hidden accounts, but the UI ignores them.
    Re-select an account on the Settings Accounts tab to restore visibility."""
    a = get_settings().accounts
    return {x for x in (*a.live, *a.evals, *a.simulation) if x}


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
def test_provider() -> dict[str, Any]:
    """Probe the currently-selected AI provider with a cheap, no-credit call.
    UI surfaces this in a green/red badge.

    Route name is historical ('/test/ollama'); kept so the frontend doesn't
    have to learn a new path. Dispatches based on settings.ai_backend.provider.
    """
    import requests
    s = get_settings().ai_backend
    provider = s.provider
    t0 = time.monotonic()

    if provider == "ollama":
        url = s.ollama_url
        probe = (url[: -len("/api/generate")] + "/api/tags") if url.endswith("/api/generate") else url
        try:
            r = requests.get(probe, timeout=5)
            r.raise_for_status()
        except Exception as e:
            return {"ok": False, "provider": provider, "error": str(e), "probed": probe}
        try:
            tags = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            tags = []
        return {
            "ok": True, "provider": provider, "probed": probe,
            "latency_s": round(time.monotonic() - t0, 3),
            "models": tags,
            "configured_model_present": s.model in tags,
            "configured_model": s.model,
        }

    if provider == "claude":
        if not s.claude_api_key:
            return {"ok": False, "provider": provider, "error": "No Claude API key configured."}
        try:
            # Cheapest probe: list available models (Messages-API metadata endpoint).
            r = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": s.claude_api_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            return {"ok": False, "provider": provider, "error": str(e)}
        tags = [m["id"] for m in r.json().get("data", [])]
        return {
            "ok": True, "provider": provider, "probed": "api.anthropic.com/v1/models",
            "latency_s": round(time.monotonic() - t0, 3),
            "models": tags,
            "configured_model_present": s.claude_model in tags,
            "configured_model": s.claude_model,
        }

    if provider == "openai":
        if not s.openai_api_key:
            return {"ok": False, "provider": provider, "error": "No OpenAI API key configured."}
        try:
            r = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {s.openai_api_key}"},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            return {"ok": False, "provider": provider, "error": str(e)}
        tags = [m["id"] for m in r.json().get("data", [])]
        return {
            "ok": True, "provider": provider, "probed": "api.openai.com/v1/models",
            "latency_s": round(time.monotonic() - t0, 3),
            "models": tags,
            "configured_model_present": s.openai_model in tags,
            "configured_model": s.openai_model,
        }

    return {"ok": False, "error": f"unknown provider: {provider}"}
