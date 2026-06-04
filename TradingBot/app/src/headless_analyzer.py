"""Headless (text-only) trade-proposal generator.

Replaces the auto-analyzer's stub. Triggered by armed bar arrivals on
``/api/feed/bar``: pulls a context window from feed.db, calls the
workstation Ollama with a numeric/textual prompt (no screenshot),
parses the structured proposal, and persists it via signal_storage so
it surfaces in the dashboard's Signal Analysis page.

Auto-trigger has no human at the chart, so this is the primary path
for unattended analysis. The hotkey-driven HelmAnalyzer + screenshot
pipeline still owns the manual case.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import proposal_sanity, runtime_config, signal_storage

logger = logging.getLogger(__name__)

# Fallback label for the persisted record's "model" field when the provider
# response didn't include one. Real model name comes from local_llm_analyzer.
FALLBACK_MODEL_LABEL = "unknown"

# How many recent bars to include in the prompt context. 60 × 5m = 5h
# of market data, which is a reasonable lookback for a 5m chart.
DEFAULT_BAR_COUNT = 60

# EMA period — must match the chart stack (see TradingBot/CLAUDE.md
# "Chart conventions"). The user's charts use only EMA(90).
EMA_PERIOD = 90
ATR_PERIOD = 14

DATA_DIR     = Path(__file__).resolve().parent.parent / "data"
FEED_DB_PATH = DATA_DIR / "feed.db"
SIGNALS_LOG  = DATA_DIR / "signals.jsonl"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
PROMPT_PATH  = Path(__file__).resolve().parent.parent / "prompts" / "headless_analyzer.txt"
# Vision prompt is the same one the manual snipping pipeline uses -- includes
# the chart vocabulary + ATM strategy menu. Used when a fresh HelmFeed
# screenshot is available for the (instrument, period).
VISION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyzer.txt"

# Maximum age of an auto-screenshot to be considered "fresh enough" for the
# bar we're analyzing. Two minutes covers any bar period from 1m up; older
# than that and we fall back to text-only.
SCREENSHOT_MAX_AGE_S = 120


def _safe_seg(s: str) -> str:
    return "".join(c if c.isalnum() or c in "_.-" else "_" for c in s)


def _latest_auto_screenshot(instrument: str, period: str) -> Path | None:
    """Return the auto_{instrument}_{period}.png that feed.py drops on each
    fresh bar, or None if absent or too stale to trust."""
    path = SCREENSHOTS_DIR / f"auto_{_safe_seg(instrument)}_{_safe_seg(period)}.png"
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > SCREENSHOT_MAX_AGE_S:
        logger.info("[headless] screenshot for %s @ %s is %.0fs old (>%.0fs); "
                    "falling back to text-only", instrument, period, age,
                    SCREENSHOT_MAX_AGE_S)
        return None
    return path


def _has_active_trade(instrument: str) -> bool:
    """True if a directional trade for *instrument* is currently live.

    "Live" = entry has triggered (or an order is working/filled) and no final
    outcome has been recorded yet. Auto-analysis uses this to skip generating a
    new signal on an instrument we're already in, so the bot doesn't stack
    positions. Flat proposals and resolved/deleted trades never count.
    """
    try:
        signals = signal_storage.load_all(SIGNALS_LOG)
    except FileNotFoundError:
        return False
    for rec in signals.values():
        if rec.get("deleted"):
            continue
        proposal = rec.get("proposal") or {}
        if proposal.get("direction") == "flat":
            continue
        if (rec.get("instrument") or proposal.get("instrument")) != instrument:
            continue
        result = (rec.get("outcome") or {}).get("result")
        if result and result != "pending":
            continue  # resolved -> not active
        exec_state = (rec.get("exec") or {}).get("state")
        if rec.get("entry_triggered") or exec_state in ("working", "filled"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public entry point — called by auto_analyzer._run_analysis
# ---------------------------------------------------------------------------

def analyze(instrument: str, period: str, bar_ts: int,
            *, bar_count: int = DEFAULT_BAR_COUNT) -> dict | None:
    """Build numeric context, pick the analysis path, persist the proposal.

    Two paths:
      - Visual: when HelmFeed has dropped a recent screenshot for this
        (instrument, period), hand it + a brief context block to the
        provider-dispatching analyzer (Ollama/Claude/OpenAI, includes
        ATM strategy menu, applies tick rounding).
      - Text-only: legacy fallback when no fresh screenshot is available.
        Uses the headless_analyzer.txt prompt with the bar table.

    Returns the persisted signal record, or None if context was insufficient
    (e.g., fewer than 20 bars in feed.db — too thin to analyze) or if no AI
    provider is configured.
    """
    # No provider configured -> skip silently with a single log line. Avoids
    # a flood of HTTP errors against api.anthropic.com / api.openai.com /
    # localhost-without-Ollama, and avoids burning resources building the
    # prompt + reading the screenshot for a call that can't happen.
    ok, why = runtime_config.is_provider_configured()
    if not ok:
        logger.info("[headless] %s @ %s: %s -- skipping", instrument, period, why)
        return None

    # Don't analyze an instrument we already hold a live trade in -- avoids
    # stacking signals (and auto-exec orders) on top of an open position.
    if _has_active_trade(instrument):
        logger.info("[headless] %s @ %s: active trade open for this instrument "
                    "-- skipping analysis", instrument, period)
        return None

    bars = _recent_bars(instrument, period, bar_ts, bar_count)
    if len(bars) < 20:
        logger.warning(
            "[headless] %s @ %s: only %d bars in feed.db; need >=20 — skipping",
            instrument, period, len(bars))
        return None

    context = _build_context(instrument, period, bars)
    shot_path = _latest_auto_screenshot(instrument, period)

    if shot_path:
        return _analyze_visual(instrument, period, bar_ts, context, shot_path)
    return _analyze_text(instrument, period, bar_ts, context, len(bars))


def _analyze_visual(instrument: str, period: str, bar_ts: int,
                    context: dict, shot_path: Path) -> dict | None:
    """Vision-LLM path: call the same provider-dispatched analyzer the manual
    snip pipeline uses. Reuses analyzer.txt (vocabulary + ATM menu + JSON
    schema) so visual headless proposals are shape-compatible with hotkey
    proposals downstream."""
    import shutil
    from . import local_llm_analyzer  # local import: avoids dep cycle in tests

    market_ctx_block = _format_visual_context_block(instrument, period, context)
    template = VISION_PROMPT_PATH.read_text(encoding="utf-8")
    full_prompt = market_ctx_block + "\n\n---\n\n" + template

    started = time.monotonic()
    try:
        result = local_llm_analyzer.analyze(shot_path, full_prompt, instrument)
    except Exception:
        logger.exception("[headless-vision] analyze() failed for %s %s",
                         instrument, period)
        return None

    # Snapshot the screenshot under a per-signal filename so the signal's
    # record points at an immutable file. The 'auto_{instr}_{period}.png'
    # latest-cache gets overwritten on every bar close; without this copy,
    # every historical signal's screenshot_path would show whatever bar
    # closed most recently. Stamp uses ms granularity since two armed
    # combos can fire within the same second.
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    safe_i = _safe_seg(instrument)
    safe_p = _safe_seg(period)
    archived = SCREENSHOTS_DIR / f"headless_{safe_i}_{safe_p}_{stamp}.png"
    try:
        shutil.copy2(shot_path, archived)
    except Exception:
        logger.exception("[headless-vision] could not archive screenshot; "
                         "falling back to the volatile auto_*.png path")
        archived = shot_path  # less ideal but still resolves on-disk

    proposal = result["proposal"]
    proposal["headless"]   = True
    proposal["bar_ts"]     = bar_ts
    proposal["instrument"] = instrument

    is_valid, reason = proposal_sanity.sanity_check(proposal)

    record = {
        "instrument":          instrument,
        "screenshot_path":     str(archived),
        "screenshot_filename": archived.name,
        "proposal":            proposal,
        "raw_response":        result.get("raw_response"),
        "duration_s":          round(time.monotonic() - started, 2),
        "model_duration_s":    round(result.get("duration_s") or 0, 2),
        "model":               proposal.get("model", FALLBACK_MODEL_LABEL),
        "provider":            proposal.get("provider", "ollama"),
        "trigger":             "headless",
        "headless_period":     period,
        "headless_bar_ts":     bar_ts,
        "headless_vision":     True,
        "market_context":      context,
    }
    if not is_valid:
        record["deleted"]               = True
        record["auto_dismissed"]        = True
        record["auto_dismissed_reason"] = reason
        logger.warning("[headless-vision] auto-dismissing %s @ %s — %s",
                       instrument, period, reason)
    persisted = signal_storage.append_signal(SIGNALS_LOG, record)
    logger.info(
        "[headless-vision] proposal stored: %s %s direction=%s atm=%s",
        instrument, period, proposal.get("direction"), proposal.get("atm_strategy"))
    return persisted


def _analyze_text(instrument: str, period: str, bar_ts: int,
                  context: dict, bar_count: int) -> dict | None:
    """Text-only path. Used when no fresh HelmFeed screenshot exists for
    the (instrument, period). Dispatches through local_llm_analyzer so it
    honors the Settings provider (Ollama / Claude / OpenAI) like every
    other analysis path."""
    from . import local_llm_analyzer  # local import: avoids dep cycle in tests

    prompt = _render_prompt(instrument, period, context, bar_count)

    started = time.monotonic()
    try:
        result = local_llm_analyzer.analyze_text(prompt, instrument)
    except Exception:
        logger.exception("[headless] LLM call failed for %s %s", instrument, period)
        return None

    # analyze_text already parsed, picked an ATM, derived stop/target, rounded,
    # and computed R:R -- mirror of the visual path. We only stamp metadata.
    proposal = result["proposal"]
    raw = result["raw_response"]
    proposal["headless"]   = True
    proposal["bar_ts"]     = bar_ts
    proposal["instrument"] = instrument

    is_valid, reason = proposal_sanity.sanity_check(proposal)

    record = {
        "instrument":       instrument,
        "screenshot_path":  None,
        "screenshot_filename": None,
        "proposal":         proposal,
        "raw_response":     raw,
        "duration_s":       round(time.monotonic() - started, 2),
        "model_duration_s": round(result.get("duration_s") or 0, 2),
        "model":            result.get("model", FALLBACK_MODEL_LABEL),
        "provider":         result.get("provider", "ollama"),
        "trigger":          "headless",
        "headless_period":  period,
        "headless_bar_ts":  bar_ts,
        "market_context":   context,
    }
    if not is_valid:
        record["deleted"]               = True
        record["auto_dismissed"]        = True
        record["auto_dismissed_reason"] = reason
        logger.warning(
            "[headless] auto-dismissing %s @ %s — %s",
            instrument, period, reason,
        )
    persisted = signal_storage.append_signal(SIGNALS_LOG, record)
    logger.info(
        "[headless] proposal stored: %s %s direction=%s atm=%s",
        instrument, period, proposal.get("direction"), proposal.get("atm_strategy"))
    return persisted


def _format_visual_context_block(instrument: str, period: str, context: dict) -> str:
    """Brief authoritative-context block for the visual path. Mirrors what
    pipeline._format_context_for_prompt produces for the manual snip flow,
    but smaller -- the LLM has the chart bitmap so we don't need to dump
    every level."""
    lines = [
        "## Authoritative Market Context (from HelmFeed bars)",
        f"Instrument: {instrument}",
        f"Period: {period}",
        f"Current price (last close): {context.get('current_price')}",
        f"Recent {context.get('bar_count')}-bar high: {context.get('period_high')}",
        f"Recent {context.get('bar_count')}-bar low:  {context.get('period_low')}",
        f"EMA({EMA_PERIOD}): {context.get('ema_value')}",
        f"ATR({ATR_PERIOD}): {context.get('atr_value')}",
        "",
        "Use the prices above as authoritative -- do not re-read them from the chart axis. "
        "Use the chart screenshot for structural interpretation (trend, pullbacks, "
        "support/resistance) only.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _recent_bars(instrument: str, period: str, bar_ts: int,
                 limit: int) -> list[tuple[int, float, float, float, float, int]]:
    """Pull the most recent <= limit bars at-or-before bar_ts. Oldest first."""
    if not FEED_DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{FEED_DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, o, h, l, c, v FROM bars "
            "WHERE instrument = ? AND period = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT ?",
            (instrument, period, bar_ts, limit),
        ).fetchall()
    finally:
        conn.close()
    rows.reverse()    # oldest first for the LLM
    return rows


def _build_context(instrument: str, period: str,
                   bars: list[tuple[int, float, float, float, float, int]]) -> dict:
    closes  = [b[4] for b in bars]
    highs   = [b[2] for b in bars]
    lows    = [b[3] for b in bars]
    current = closes[-1]
    period_high = max(highs)
    period_low  = min(lows)
    ema = _ema(closes, EMA_PERIOD)
    atr = _atr(highs, lows, closes, ATR_PERIOD)

    # Compact table: ts | o h l c v. Use ISO time so the LLM can reason
    # about session timing.
    rows = []
    for ts, o, h, l, c, v in bars:
        t = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{t} | o={o:.2f} h={h:.2f} l={l:.2f} c={c:.2f} v={v}")
    bar_table = "\n".join(rows)

    return {
        "current_price": current,
        "period_high":   period_high,
        "period_low":    period_low,
        "ema_value":     round(ema, 2) if ema is not None else None,
        "atr_value":     round(atr, 2) if atr is not None else None,
        "bar_count":     len(bars),
        "bar_table":     bar_table,
    }


def _render_prompt(instrument: str, period: str, ctx: dict, bar_count: int) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(
        instrument    = instrument,
        period        = period,
        current_price = ctx["current_price"],
        period_high   = ctx["period_high"],
        period_low    = ctx["period_low"],
        ema_period    = EMA_PERIOD,
        ema_value     = ctx["ema_value"],
        atr_value     = ctx["atr_value"],
        bar_count     = bar_count,
        bar_table     = ctx["bar_table"],
    )


# ---------------------------------------------------------------------------
# Indicator math (kept here, not in instruments.py, to avoid spreading the
# Core layer across files; small, vetted formulas only).
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period   # SMA seed
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int) -> float | None:
    if len(highs) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i-1]),
            abs(lows[i]   - closes[i-1]),
        )
        trs.append(tr)
    # Wilder's smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr
