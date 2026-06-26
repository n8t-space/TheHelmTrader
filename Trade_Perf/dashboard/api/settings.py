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
    # Global provider selector + per-component overrides. Each component falls
    # back to `provider` when its override is "". Lets News (large HTML
    # extraction) run on a cloud model while signal analysis stays on local
    # Ollama, etc. Credentials/models below are shared per provider.
    provider: str = Field(default="ollama", pattern="^(ollama|claude|openai)$")
    news_provider:   str = Field(default="", pattern="^(ollama|claude|openai|)$")
    signal_provider: str = Field(default="", pattern="^(ollama|claude|openai|)$")
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


class AccountConfig(BaseModel):
    """Per-account trading config (Item 3). Keyed by NT account id in
    Settings.account_configs, rendered as a card for LIVE + EVAL accounts only
    (Sim falls back to the global auto_trader defaults). Secret-free -- ids live
    in credentials.json; this map holds only a label + numeric limits.

    These supersede the matching global auto_trader guardrails when set; the
    global fields remain the default fallback (and the SOLE source for Sim
    accounts, which have no card). See effective_guardrails()."""
    # User-chosen label for this account's trading config. Display falls back to
    # accounts.names[id] then the raw id.
    name: str = Field(default="")
    # USER-ENTERED base cash for this account ("cash now" as of the save below).
    # The config card's CURRENT cash = base_cash + realized P&L of this account's
    # trades that closed AFTER cash_basis_ts (Trade Performance / trades.db). This
    # replaces the broker NetLiquidation pull, which was wrong/empty whenever no
    # Auto-Trader strategy was running on the account. 0 => no basis set (sizing
    # in percent mode then has no cash source -- see _risk_sized_qty fallback).
    base_cash: float = Field(default=0.0, ge=0.0)
    # UTC ISO stamp (seconds) of when base_cash was last saved. The entered number
    # is the account cash AS OF this instant; only trades closing at/after it
    # adjust the computed current cash. Stamped server-side on every base_cash
    # change (settings write path); not user-edited directly.
    cash_basis_ts: str = Field(default="")
    # Risk per trade. interpretation: "percent" -> value is % of CURRENT cash
    # (base_cash + realized since basis); "price" -> a fixed risk amount in
    # account-currency dollars.
    risk_per_trade_value: float = Field(default=0.0, ge=0.0)
    risk_per_trade_mode: str = Field(default="percent", pattern="^(percent|price)$")
    max_daily_loss: float = Field(default=0.0, ge=0.0)             # 0 => off
    max_concurrent_per_instrument: int = Field(default=1, ge=1, le=20)
    max_contracts_per_instrument: int = Field(default=1, ge=1, le=50)
    stop_if_balance_below: float = Field(default=0.0, ge=0.0)      # 0 => off
    # USER-ENTERED trailing max drawdown limit in account-currency dollars
    # (e.g. 2500.0). Tracked against a server-computed equity high-water mark
    # (auto_trader.report_balance). 0 => off.
    trailing_dd_limit: float = Field(default=0.0, ge=0.0)
    # USER-ENTERED profit target to PASS an evaluation, in account-currency
    # dollars (e.g. 3000.0). Eval-account concept only; ignored for other
    # buckets. 0 => unset. Surfaced as a column on the Accounts tab.
    profit_target: float = Field(default=0.0, ge=0.0)


class Accounts(BaseModel):
    # Bucket your NT account IDs so the Home page's cumulative-earnings card
    # aggregates correctly. Set these via the Settings page after first run.
    # NT's default sim accounts (Sim101, Playback101, Backtest, SimBetaSIM) are
    # pre-listed under 'simulation' because every NT install has them; the
    # other buckets start empty.
    live: list[str] = Field(default_factory=list)
    evals: list[str] = Field(default_factory=list)
    # PA = Paid Account (a passed eval -> funded). Real-money bucket, sibling to
    # live. An eval graduates here once it passes.
    paid: list[str] = Field(default_factory=list)
    simulation: list[str] = Field(default_factory=lambda: [
        "Sim101", "Playback101", "Backtest", "SimBetaSIM",
    ])
    # Friendly display names keyed by NT account ID. Display-only -- the UI
    # shows the name (falling back to the raw ID) wherever an account appears.
    # Empty/whitespace names are dropped client-side.
    names: dict[str, str] = Field(default_factory=dict)
    # Entity ownership keyed by NT account ID: "personal" | "llc". Drives the
    # business vs personal split for expense/P&L attribution. Unset -> personal.
    entities: dict[str, str] = Field(default_factory=dict)


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
    # When False (default, Item 1A / D1) ATM is OPTIONAL: a directional proposal
    # with no atm_strategy is accepted as long as it carries valid numeric
    # stop/target and the auto-trader executes it via the bare-LIMIT OCO path.
    # When True the legacy behavior returns: blank-ATM directional proposals are
    # rejected. Reversible kill-switch. Read live (no restart).
    require_atm_for_directional: bool = False
    # Final fallback contract count for an ATM-less directional trade when risk
    # sizing can't run (no per-account config, missing cash/stop/tick_value) and
    # the proposal carried no explicit qty. Risk sizing (Item 3) is the primary
    # source; this only bottoms out the cascade above the hard default of 1.
    default_qty: int = Field(default=1, ge=1, le=50)
    max_contracts_per_order: int = Field(default=2, ge=1, le=50)
    max_concurrent: int = Field(default=1, ge=1, le=20)
    # Daily realized-loss cutoff in account-currency dollars. 0 => off. Once
    # session realized P&L on `account` <= -cutoff, the strategy stops placing
    # and disarms remaining signals.
    daily_loss_cutoff: float = Field(default=0.0, ge=0.0)
    # Account-balance floor (live equity, account-currency dollars). 0 => off.
    # When the strategy reports account equity <= this, auto-trading is forced
    # OFF (master switch) and the queue empties -- a hard fail-safe that requires
    # manual re-enable. Open positions keep their own ATM stop (not flattened).
    min_account_balance: float = Field(default=0.0, ge=0.0)
    poll_seconds: int = Field(default=3, ge=1, le=60)
    entry_window_minutes: int = Field(default=240, ge=1, le=1440)
    # Stamped (naive-local ISO) the moment auto-trading goes OFF->ON. Autonomous
    # execution only picks up signals created at/after this, so flipping the
    # switch never replays a backlog. Managed automatically; not user-edited.
    enabled_at: str = Field(default="")
    # When True, an auto-entered trade's fill grabs HelmFeed's latest chart
    # screenshot for that instrument and stashes it on the signal exec, so the
    # per-trade Journal can show the chart at entry. Opt-in; default OFF.
    capture_entry_screenshot: bool = False


class NewsSource(BaseModel):
    """A user-configurable economic-calendar source (Item 7).

    type semantics (per-source parsing adapter in news.py):
      xml         -- fetch + XML-parse using the ForexFactory feed schema.
                     A non-FF XML feed needs its own adapter (v1 supports the
                     FF schema only).
      scrape      -- fetch HTML, hand to the AI extractor (the Econoday path).
      ai-extract  -- alias of scrape for pages that explicitly require AI
                     extraction; same code path (kept in the enum for forward
                     flexibility).
    Secret-free: URLs only -- AI keys stay in credentials.json ai_backend."""
    name: str = Field(min_length=1)             # unique key + display label
    url: str = Field(default="")
    # factbase = Roll Call / Factba.se presidential-schedule CSV (speaking events).
    type: str = Field(pattern="^(xml|scrape|ai-extract|factbase)$")
    enabled: bool = True


# Defaults seeded into news.sources by _migrate_news_sources when the list is
# empty/absent. URLs live here (and historically in news.py) so the migration
# can build the two legacy sources without importing the router.
_FF_DEFAULT_URL       = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
_ECONODAY_DEFAULT_URL = "https://us.econoday.com/byweek?cust=us&lid=0"


class News(BaseModel):
    """Economic-calendar widget config.

    Sources are a user-editable list (Item 7): each {name, url, type, enabled}.
    The two built-in sources (ForexFactory XML, Econoday scrape) are seeded as
    defaults and may be edited / disabled / removed or joined by new ones.

    Filter rules apply to the merged event list before render. Empty
    impact_filter / currency_filter == no filter.
    """
    enabled: bool = True
    # DEPRECATED (2.0.0): superseded by `sources`. Kept READABLE for the whole
    # 2.0.x line so a rollback to a pre-2.0 build still finds them; the UI writes
    # only `sources` going forward. Dropped in a later minor (see MIGRATION.md).
    forexfactory_enabled: bool = True
    econoday_enabled: bool = True
    sources: list[NewsSource] = Field(default_factory=list)
    impact_filter: list[str] = Field(default_factory=lambda: ["High"])
    currency_filter: list[str] = Field(default_factory=lambda: ["USD"])
    refresh_interval_minutes: int = Field(default=15, ge=5, le=180)


class Auditor(BaseModel):
    """Signal <-> NT8 fill integrity auditor. Reconciles each executed signal's
    paper P&L against the real broker fills; NT db is ground truth. Mismatches on
    confidently-linked trades are auto-corrected to the real net, unlinked fills
    are flagged for review. Runs on the interval below (default hourly)."""
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=5, le=1440)


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class BlackoutWindow(BaseModel):
    """A daily time-of-day range (operator timezone) where automation pauses.
    start>end spans midnight (e.g. 22:00->06:00). start==end is ignored."""
    start: str = Field(pattern=_HHMM)
    end: str = Field(pattern=_HHMM)
    label: str = ""


class Automation(BaseModel):
    """Global automation pauses. During a blackout window neither signal
    generation NOR auto-execution runs (open positions keep their own ATM)."""
    blackout_windows: list[BlackoutWindow] = Field(default_factory=list)


class Tax(BaseModel):
    """Estimated tax on realized futures P&L. Futures are IRC Section 1256
    contracts: gains are taxed 60% at long-term, 40% at short-term/ordinary
    rates regardless of holding period. The blended effective rate is
    0.60*lt_rate + 0.40*st_rate + state_rate (state typically taxes all gains
    as ordinary, outside the 60/40 split). Rates are fractions (0.20 = 20%).
    This is an ESTIMATE, not tax advice -- it ignores year-end mark-to-market
    of open positions, loss carrybacks, and the wash-sale exemption nuances."""
    enabled: bool = True
    lt_rate: float = Field(default=0.20, ge=0.0, le=1.0)     # long-term cap gains
    st_rate: float = Field(default=0.37, ge=0.0, le=1.0)     # short-term / ordinary
    state_rate: float = Field(default=0.0, ge=0.0, le=1.0)   # flat state, on all gains

    @property
    def blended_rate(self) -> float:
        return round(0.60 * self.lt_rate + 0.40 * self.st_rate + self.state_rate, 6)


class Settings(BaseModel):
    schema_version: int = SCHEMA_VERSION
    appearance: Appearance = Field(default_factory=Appearance)
    ai_backend: AiBackend = Field(default_factory=AiBackend)
    strategy: Strategy = Field(default_factory=Strategy)
    accounts: Accounts = Field(default_factory=Accounts)
    # Per-account trading config (Item 3), keyed by NT account id. Secret-free;
    # ids live in credentials.json. Empty by default -> every account falls back
    # to the global auto_trader guardrails.
    account_configs: dict[str, AccountConfig] = Field(default_factory=dict)
    # User-entered commission keyed by NT master instrument symbol (e.g. "MES",
    # "MCL"), mirroring NT8's commission templates: a per-contract rate charged
    # PER SIDE (each execution). When a symbol's rate is > 0 it OVERRIDES the
    # commission NT8 booked on that instrument's fills in round-trip P&L
    # (trades.derive_trades applies rate x contracts x sides per trade);
    # 0/absent falls back to the NT8-reported commission. Lets Sim/Eval fills --
    # where NT8 books $0 -- reflect the real per-instrument cost. The
    # exchange/regulatory `fee` on the fills is left untouched. Not secret, so
    # this lives in settings.json (unlike the per-account `accounts` section).
    commissions: dict[str, float] = Field(default_factory=dict)
    news: News = Field(default_factory=News)
    auto_trader: AutoTrader = Field(default_factory=AutoTrader)
    auditor: Auditor = Field(default_factory=Auditor)
    automation: Automation = Field(default_factory=Automation)
    tax: Tax = Field(default_factory=Tax)
    # Display name for the business entity, used wherever an account/expense is
    # tagged "llc". Blank -> the UI just shows "LLC".
    llc_name: str = Field(default="")


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


def _migrate_news_sources(raw: dict) -> dict:
    """Seed news.sources from the legacy forexfactory_enabled / econoday_enabled
    booleans when the list is empty/absent (Item 7). Mirrors the
    _migrate_credentials pattern: runs on every load, no-op once `sources` has
    entries. The two booleans STAY readable for the 2.0.x line (rollback); the
    UI writes only `sources` going forward."""
    news = raw.get("news")
    if not isinstance(news, dict):
        return raw
    if news.get("sources"):
        return raw
    ff_on = news.get("forexfactory_enabled", True)
    ed_on = news.get("econoday_enabled", True)
    news["sources"] = [
        {"name": "ForexFactory", "url": _FF_DEFAULT_URL,
         "type": "xml", "enabled": bool(ff_on)},
        {"name": "Econoday", "url": _ECONODAY_DEFAULT_URL,
         "type": "scrape", "enabled": bool(ed_on)},
    ]
    logger.info("[settings] seeded news.sources from legacy booleans "
                "(ff=%s econoday=%s)", ff_on, ed_on)
    return raw


def _load_from_disk() -> Settings:
    raw = _read_json(SETTINGS_PATH)
    raw = _migrate_credentials(raw)
    raw = _migrate_news_sources(raw)
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


def _hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def in_blackout(now: datetime | None = None) -> tuple[bool, str]:
    """Is automation currently paused by a blackout window? Returns
    (paused, label). Times are operator-local HH:MM; a window with start>end
    spans midnight. Cheap; safe in the feed + exec hot paths."""
    windows = get_settings().automation.blackout_windows
    if not windows:
        return False, ""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for w in windows:
        try:
            a, b = _hm_to_min(w.start), _hm_to_min(w.end)
        except (ValueError, AttributeError):
            continue
        if a == b:
            continue
        inside = (a <= cur < b) if a < b else (cur >= a or cur < b)  # overnight
        if inside:
            return True, (w.label or f"{w.start}-{w.end}")
    return False, ""


def visible_accounts() -> set[str]:
    """Source-of-truth set for which NT account IDs are visible to the rest of
    the site. Union of the Settings buckets (live + evals + paid + simulation).
    Any account not in this set is hidden from /api/dimensions, FilterBar,
    Home cumulative-earnings, and default unfiltered fill queries -- the
    recorder keeps writing fills for hidden accounts, but the UI ignores them.
    Re-select an account on the Settings Accounts tab to restore visibility."""
    a = get_settings().accounts
    return {x for x in (*a.live, *a.evals, *a.paid, *a.simulation) if x}


def instrument_commissions() -> dict[str, float]:
    """User-entered per-instrument commission, in dollars per contract PER SIDE,
    keyed by NT master instrument symbol (e.g. "MES"). Mirrors NT8 commission
    templates. Only positive rates are returned. Consumed by
    trades.derive_trades to override the NT8-reported commission in round-trip
    P&L (rate x contracts x sides per trade); instruments absent here keep the
    fills' commission. Cheap; safe in hot paths."""
    return {s: r for s, r in get_settings().commissions.items()
            if isinstance(r, (int, float)) and r > 0}


def auto_trader_config() -> AutoTrader:
    """Current Auto-Trader config. Single source of truth for whether execution
    is enabled, which account is locked, and the risk guardrails. Consumed by
    the auto_trader router (arm gating + /exec/queue) and surfaced to the NT8
    strategy via that queue. Cheap; safe in hot paths."""
    return get_settings().auto_trader


def account_config(account_id: str) -> AccountConfig | None:
    """Per-account trading config (Item 3) for an NT account id, or None when
    the account has no card (every Sim account per D6, and any LIVE/EVAL account
    the user hasn't configured). Cheap; safe in hot paths."""
    if not account_id:
        return None
    return get_settings().account_configs.get(account_id)


class ResolvedGuardrails(BaseModel):
    """Effective per-account guardrails after layering the per-account config
    over the global auto_trader defaults (Item 4 / D3). An account WITH a config
    uses each set field (> 0 / non-default) else the global default; an account
    WITHOUT a config (incl. every Sim account per D6) resolves entirely to the
    global defaults. trailing_dd_limit has no global equivalent (off when
    unset / on Sim)."""
    max_contracts_per_instrument: int
    max_concurrent_per_instrument: int
    max_daily_loss: float
    stop_if_balance_below: float
    trailing_dd_limit: float
    risk_per_trade_value: float
    risk_per_trade_mode: str
    has_account_config: bool


def effective_guardrails(account_id: str) -> ResolvedGuardrails:
    """Resolve the guardrails the Auto-Trader enforces for `account_id`. Every
    enforcement point calls this instead of reading a global field directly so a
    per-account config supersedes the global default while a Sim/unconfigured
    account falls back to the globals (D3 / D6)."""
    at = auto_trader_config()
    # getattr fallbacks so a partial test-mock of auto_trader_config() (and
    # forward-compat) can't break the resolver.
    g_contracts = getattr(at, "max_contracts_per_order", 2)
    g_concurrent = getattr(at, "max_concurrent", 1)
    g_daily = getattr(at, "daily_loss_cutoff", 0.0)
    g_floor = getattr(at, "min_account_balance", 0.0)
    cfg = account_config(account_id)
    if cfg is None:
        return ResolvedGuardrails(
            max_contracts_per_instrument=g_contracts,
            max_concurrent_per_instrument=g_concurrent,
            max_daily_loss=g_daily,
            stop_if_balance_below=g_floor,
            trailing_dd_limit=0.0,
            risk_per_trade_value=0.0,
            risk_per_trade_mode="percent",
            has_account_config=False,
        )
    return ResolvedGuardrails(
        # Per-account contract cap defaults to 1; treat 1 as "set" only when the
        # user raised it, else fall back to the global per-order cap so an
        # unconfigured-but-present card doesn't silently tighten to 1.
        max_contracts_per_instrument=(cfg.max_contracts_per_instrument
                                      if cfg.max_contracts_per_instrument > 1
                                      else g_contracts),
        max_concurrent_per_instrument=cfg.max_concurrent_per_instrument,
        max_daily_loss=(cfg.max_daily_loss if cfg.max_daily_loss > 0
                        else g_daily),
        stop_if_balance_below=(cfg.stop_if_balance_below if cfg.stop_if_balance_below > 0
                               else g_floor),
        trailing_dd_limit=cfg.trailing_dd_limit,
        risk_per_trade_value=cfg.risk_per_trade_value,
        risk_per_trade_mode=cfg.risk_per_trade_mode,
        has_account_config=True,
    )


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


def _utc_now_iso() -> str:
    """UTC ISO seconds with a trailing Z -- matches trades.db exit_time so the
    current-cash computation can compare cash_basis_ts against trade close times
    without timezone gymnastics."""
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp_cash_basis(settings: Settings, prev: Settings | None) -> None:
    """Stamp account_configs[id].cash_basis_ts = now whenever the user-entered
    base_cash changed (or was first set), so the entered number means "cash AS OF
    this save". A write that leaves base_cash unchanged preserves the prior stamp.
    Done server-side here (not on the client) so the basis can't be back-dated and
    always matches the trades.db time format (UTC Z)."""
    prev_cfgs = prev.account_configs if prev else {}
    for acct_id, cfg in settings.account_configs.items():
        old = prev_cfgs.get(acct_id)
        old_base = old.base_cash if old else 0.0
        old_ts = old.cash_basis_ts if old else ""
        if cfg.base_cash != old_base:
            # base_cash moved (incl. first-time set / reset to 0) -> new basis now.
            cfg.cash_basis_ts = _utc_now_iso() if cfg.base_cash > 0 else ""
        elif not cfg.cash_basis_ts and old_ts:
            # Unchanged base_cash but the client dropped the stamp -> preserve it.
            cfg.cash_basis_ts = old_ts


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
        _stamp_cash_basis(settings, prev)
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
