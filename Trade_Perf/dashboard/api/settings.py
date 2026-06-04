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
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Per-user config dir. Override with HELM_HOME to run an isolated instance
# (dev environment) that won't touch the live ~/.helm settings/credentials.
HELM_HOME = Path(os.environ.get("HELM_HOME") or (Path.home() / ".helm"))
SETTINGS_PATH = HELM_HOME / "settings.json"
SCHEMA_VERSION = 1

# Sensitive, machine-specific config kept OUT of settings.json so the latter is
# safe to share / commit / bundle: API keys + inference URLs (ai_backend) and
# the user's broker account IDs (accounts). These live in credentials.json
# beside settings.json, which is git-ignored, never overwritten by install/
# update, and never included in support bundles.
_CREDENTIAL_SECTIONS = ("ai_backend", "accounts")


def _credentials_path() -> Path:
    """Path to credentials.json, derived from SETTINGS_PATH so that tests which
    redirect SETTINGS_PATH to a temp dir automatically isolate credentials too
    (do NOT make this a module constant -- it must follow SETTINGS_PATH)."""
    return SETTINGS_PATH.with_name("credentials.json")


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[settings] failed to read %s (%s)", path, e)
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on Windows + POSIX


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
    reconciliation_cap: int = Field(default=3, ge=0, le=20)
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


class AutoTrader(BaseModel):
    """Auto-Trader (Sim-only v1). The bot automates the mechanical ATM entry for
    signals the user explicitly arms -- no autonomous firing. Hard-locked to ONE
    account. Master switch defaults OFF: nothing executes until the user enables
    it AND sets an account.

    Risk guardrails enforced jointly by the dashboard (arming) and the NT8
    HelmAutoTrader strategy (placement): account lock, max contracts/order, max
    concurrent open ATMs, and a daily realized-loss cutoff that auto-disarms.
    """
    enabled: bool = False
    # The single NT account the auto-trader may act on (e.g. "Sim101"). Empty
    # => disabled regardless of `enabled`. The NT strategy ALSO refuses to run
    # if its own account name != this value (defense in depth).
    account: str = Field(default="")
    max_contracts_per_order: int = Field(default=2, ge=1, le=50)
    max_concurrent: int = Field(default=1, ge=1, le=20)
    # Daily realized-loss cutoff in account-currency dollars. 0 => off. Once
    # session realized P&L on `account` <= -cutoff, the strategy stops placing
    # and disarms remaining signals.
    daily_loss_cutoff: float = Field(default=0.0, ge=0.0)
    poll_seconds: int = Field(default=3, ge=1, le=60)
    entry_window_minutes: int = Field(default=240, ge=1, le=1440)
    # Stamped (naive-local ISO) the moment auto-trading goes OFF->ON. Autonomous
    # execution only picks up signals created at/after this, so flipping the
    # switch never replays a backlog. Managed automatically; not user-edited.
    enabled_at: str = Field(default="")


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
    auto_trader: AutoTrader = Field(default_factory=AutoTrader)


# ---------------------------------------------------------------------------
# Load / save / cache
# ---------------------------------------------------------------------------

_cache: Settings | None = None
_cache_lock = Lock()


def _migrate_credentials(raw: dict) -> dict:
    """One-time split: legacy settings.json carried ai_backend/accounts inline.
    If there's no credentials.json yet but settings.json holds those sections,
    move them into credentials.json and strip settings.json of them, so secrets
    live in exactly one git-ignored place. Returns the (possibly stripped) raw
    settings dict. This runs on every load, so it also covers the post-update
    restart -- it's the precheck that exports an already-configured AI backend."""
    creds_path = _credentials_path()
    if creds_path.is_file():
        return raw
    present = {k: raw[k] for k in _CREDENTIAL_SECTIONS if k in raw}
    if not present:
        return raw
    try:
        _write_json_atomic(creds_path, present)
        stripped = {k: v for k, v in raw.items() if k not in _CREDENTIAL_SECTIONS}
        _write_json_atomic(SETTINGS_PATH, stripped)
        logger.info("[settings] migrated %s out of settings.json into %s",
                    list(present), creds_path)
        return stripped
    except OSError as e:
        logger.warning("[settings] credentials migration failed (%s); leaving inline", e)
        return raw


def _load_from_disk() -> Settings:
    raw = _read_json(SETTINGS_PATH)
    raw = _migrate_credentials(raw)
    # Overlay credentials.json over settings -- credentials are authoritative for
    # their sections. A missing credentials file just leaves the defaults.
    creds = _read_json(_credentials_path())
    merged = {**raw, **{k: v for k, v in creds.items() if k in _CREDENTIAL_SECTIONS}}
    if not merged:
        logger.info("[settings] no settings/credentials on disk; using defaults")
        return Settings()
    try:
        s = Settings.model_validate(merged)
    except ValidationError as e:
        logger.warning("[settings] invalid settings/credentials (%s); using defaults", e)
        return Settings()
    # Safety: if auto-trading is already ON but carries no enable timestamp (first
    # load after this feature shipped, or any fresh process), stamp it NOW so
    # autonomous execution starts from process start -- it never replays a backlog
    # of signals created before the server came up.
    if s.auto_trader.enabled and not s.auto_trader.enabled_at:
        s.auto_trader.enabled_at = datetime.now().isoformat(timespec="seconds")
    return s


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


def auto_trader_config() -> AutoTrader:
    """Current Auto-Trader config. Single source of truth for whether execution
    is enabled, which account is locked, and the risk guardrails. Consumed by
    the auto_trader router (arm gating + /exec/queue) and surfaced to the NT8
    strategy via that queue. Cheap; safe in hot paths."""
    return get_settings().auto_trader


def set_auto_trader_enabled(enabled: bool) -> AutoTrader:
    """Flip just the Auto-Trader master switch (the live execution toggle) and
    persist. Used by the quick checkbox on the Signal Detail card so the user
    can stage (arm) signals with execution off, then turn it on when ready."""
    updated = get_settings().model_copy(deep=True)
    updated.auto_trader.enabled = bool(enabled)
    _replace(updated)
    return updated.auto_trader


def _save_to_disk(settings: Settings) -> None:
    """Persist split across two files: secrets (ai_backend + accounts) to the
    git-ignored credentials.json, everything else to settings.json. settings.json
    therefore never contains an API key or account ID and is safe to share."""
    full = settings.model_dump()
    creds  = {k: full[k] for k in _CREDENTIAL_SECTIONS if k in full}
    public = {k: v for k, v in full.items() if k not in _CREDENTIAL_SECTIONS}
    _write_json_atomic(SETTINGS_PATH, public)
    _write_json_atomic(_credentials_path(), creds)


def _replace(settings: Settings) -> None:
    global _cache
    with _cache_lock:
        prev = _cache
        # Manage auto-trading's enabled_at so autonomous execution only acts on
        # signals created after it was enabled (no backlog replay):
        #   * OFF->ON transition -> stamp now
        #   * staying ON but a write dropped the value -> preserve the prior one
        at = settings.auto_trader
        if at.enabled:
            if prev is None or not prev.auto_trader.enabled:
                at.enabled_at = datetime.now().isoformat(timespec="seconds")
            elif not at.enabled_at and prev.auto_trader.enabled_at:
                at.enabled_at = prev.auto_trader.enabled_at
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


@router.get("/models")
def list_models(provider: str | None = None) -> dict[str, Any]:
    """Return the live model catalog for whichever provider is configured (or
    the one supplied via ?provider=...). Used by the Settings AI tab to render
    model dropdowns instead of free-text inputs.

    Cheaper than /test/ollama: no timing, no model-presence checks, just the
    catalog. Safe-fails to {ok: false, models: []} on any error so the
    frontend's dropdown falls back to a text input cleanly.
    """
    import requests
    ai = get_settings().ai_backend
    p  = (provider or ai.provider).lower()

    try:
        if p == "ollama":
            url = ai.ollama_url
            probe = (url[: -len("/api/generate")] + "/api/tags") if url.endswith("/api/generate") else url
            r = requests.get(probe, timeout=5)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {"ok": True, "provider": p, "models": sorted(models)}

        if p == "claude":
            if not ai.claude_api_key:
                return {"ok": False, "provider": p, "models": [], "error": "no API key"}
            r = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": ai.claude_api_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            r.raise_for_status()
            models = [m["id"] for m in r.json().get("data", [])]
            return {"ok": True, "provider": p, "models": sorted(models)}

        if p == "openai":
            if not ai.openai_api_key:
                return {"ok": False, "provider": p, "models": [], "error": "no API key"}
            r = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {ai.openai_api_key}"},
                timeout=10,
            )
            r.raise_for_status()
            models = [m["id"] for m in r.json().get("data", [])]
            return {"ok": True, "provider": p, "models": sorted(models)}

        return {"ok": False, "provider": p, "models": [], "error": f"unknown provider: {p}"}
    except Exception as e:  # noqa: BLE001 -- want to surface any failure to the UI
        return {"ok": False, "provider": p, "models": [], "error": str(e)}


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
