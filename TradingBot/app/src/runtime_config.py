"""Runtime-configurable knobs.

When this code runs under the dashboard's uvicorn process, it delegates to
``dashboard.api.settings.get_settings()`` so the UI's Settings page can hot-reload
values without a restart. When running standalone (e.g., ``main.py`` CLI), the
dashboard package isn't on sys.path and we fall back to hardcoded defaults that
match the prior behavior.

Add a knob: extend ``Defaults`` below + the matching Pydantic field in
``dashboard/api/settings.py`` (same name, same type). Call sites use, e.g.,
``runtime_config.ollama_url()`` -- no other plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Defaults:
    # AI backend
    provider:          str = "ollama"   # ollama | claude | openai
    # Defaults assume Ollama on the same machine. Point at a LAN host
    # via the Settings page if you've offloaded inference (e.g. a beefier
    # workstation with a GPU).
    ollama_url:        str = "http://127.0.0.1:11434/api/generate"
    model:             str = "qwen2.5vl:7b"
    fallback_model:    str = "minicpm-v:latest"
    request_timeout_s: int = 300
    num_ctx:           int = 8192
    # Cloud providers (off by default)
    claude_api_key:    str = ""
    claude_model:      str = "claude-sonnet-4-6"
    claude_max_tokens: int = 2048
    openai_api_key:    str = ""
    openai_model:      str = "gpt-4o"
    openai_max_tokens: int = 2048
    # Strategy
    reconciliation_cap: int = 3
    stale_bar_seconds:  int = 120
    retention_days:     int = 7


_D = Defaults()


def _live():
    """Return the dashboard's live settings object, or None if not importable.
    Cached lookups inside dashboard.api.settings keep this cheap."""
    try:
        from dashboard.api.settings import get_settings  # type: ignore[import-not-found]
        return get_settings()
    except Exception:
        return None


def provider() -> str:
    s = _live()
    return s.ai_backend.provider if s else _D.provider


def is_provider_configured() -> tuple[bool, str]:
    """Is the selected AI provider configured enough to call?

    Returns (ok, reason). ok=False means the analyzer should NOT make a call
    -- show a useful message instead. ok=True means we have what we need to
    attempt the request (the actual call may still fail with a transport
    error, but that's a separate concern from configuration).

    - ollama: URL must be non-empty (default LAN URL counts as configured).
    - claude/openai: API key must be non-empty.
    """
    s = _live()
    if s is None:
        # Standalone CLI without dashboard settings -- assume defaults
        # (Ollama URL is set in Defaults). User can override via env if needed.
        return True, ""

    backend = s.ai_backend
    p = backend.provider
    if p == "ollama":
        url = (backend.ollama_url or "").strip()
        if not url:
            return False, ("AI provider 'ollama' selected but no URL configured. "
                           "Set the Ollama URL in Settings -> AI Backend.")
        return True, ""
    if p == "claude":
        key = (backend.claude_api_key or "").strip()
        if not key:
            return False, ("AI provider 'claude' selected but no API key set. "
                           "Add a Claude API key in Settings -> AI Backend, or "
                           "switch the provider.")
        return True, ""
    if p == "openai":
        key = (backend.openai_api_key or "").strip()
        if not key:
            return False, ("AI provider 'openai' selected but no API key set. "
                           "Add an OpenAI API key in Settings -> AI Backend, or "
                           "switch the provider.")
        return True, ""
    return False, f"Unknown AI provider: {p!r}"


def ollama_url() -> str:
    s = _live()
    return s.ai_backend.ollama_url if s else _D.ollama_url


def claude_api_key() -> str:
    s = _live()
    return s.ai_backend.claude_api_key if s else _D.claude_api_key


def claude_model() -> str:
    s = _live()
    return s.ai_backend.claude_model if s else _D.claude_model


def claude_max_tokens() -> int:
    s = _live()
    return s.ai_backend.claude_max_tokens if s else _D.claude_max_tokens


def openai_api_key() -> str:
    s = _live()
    return s.ai_backend.openai_api_key if s else _D.openai_api_key


def openai_model() -> str:
    s = _live()
    return s.ai_backend.openai_model if s else _D.openai_model


def openai_max_tokens() -> int:
    s = _live()
    return s.ai_backend.openai_max_tokens if s else _D.openai_max_tokens


def model() -> str:
    s = _live()
    return s.ai_backend.model if s else _D.model


def fallback_model() -> str:
    s = _live()
    return s.ai_backend.fallback_model if s else _D.fallback_model


def request_timeout_s() -> int:
    s = _live()
    return s.ai_backend.request_timeout_s if s else _D.request_timeout_s


def num_ctx() -> int:
    s = _live()
    return s.ai_backend.num_ctx if s else _D.num_ctx


def reconciliation_cap() -> int:
    s = _live()
    return s.strategy.reconciliation_cap if s else _D.reconciliation_cap


def stale_bar_seconds() -> int:
    s = _live()
    return s.strategy.stale_bar_seconds if s else _D.stale_bar_seconds


def retention_days() -> int:
    s = _live()
    return s.strategy.retention_days if s else _D.retention_days
