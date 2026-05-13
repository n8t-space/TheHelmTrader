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
    ollama_url:        str = "http://<workstation-LAN-IP>:11434/api/generate"
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
    confidence_floor:   float = 0.75
    max_attempts:       int = 2
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


def confidence_floor() -> float:
    s = _live()
    return s.strategy.confidence_floor if s else _D.confidence_floor


def max_attempts() -> int:
    s = _live()
    return s.strategy.max_attempts if s else _D.max_attempts


def reconciliation_cap() -> int:
    s = _live()
    return s.strategy.reconciliation_cap if s else _D.reconciliation_cap


def stale_bar_seconds() -> int:
    s = _live()
    return s.strategy.stale_bar_seconds if s else _D.stale_bar_seconds


def retention_days() -> int:
    s = _live()
    return s.strategy.retention_days if s else _D.retention_days
