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

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

from . import instruments, proposal_sanity, signal_storage

logger = logging.getLogger(__name__)

# Reuse the same workstation endpoint as the screenshot analyzer so we
# don't fan out config across modules. qwen2.5vl handles text-only input
# fine (it's vision-language, not vision-only).
OLLAMA_URL = "http://<workstation-LAN-IP>:11434/api/generate"
MODEL      = "qwen2.5vl:7b"
TIMEOUT    = 120

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
PROMPT_PATH  = Path(__file__).resolve().parent.parent / "prompts" / "headless_analyzer.txt"


# ---------------------------------------------------------------------------
# Public entry point — called by auto_analyzer._run_analysis
# ---------------------------------------------------------------------------

def analyze(instrument: str, period: str, bar_ts: int,
            *, bar_count: int = DEFAULT_BAR_COUNT) -> dict | None:
    """Build numeric context, call the LLM, persist the proposal.

    Returns the persisted signal record, or None if context was insufficient
    (e.g., fewer than 20 bars in feed.db — too thin to analyze).
    """
    bars = _recent_bars(instrument, period, bar_ts, bar_count)
    if len(bars) < 20:
        logger.warning(
            "[headless] %s @ %s: only %d bars in feed.db; need >=20 — skipping",
            instrument, period, len(bars))
        return None

    context = _build_context(instrument, period, bars)
    prompt  = _render_prompt(instrument, period, context, len(bars))

    started = time.monotonic()
    try:
        proposal, raw, model_duration_s = _call_ollama(prompt)
    except Exception:
        logger.exception("[headless] LLM call failed for %s %s", instrument, period)
        return None

    proposal["headless"]      = True
    proposal["bar_ts"]        = bar_ts
    proposal["instrument"]    = instrument  # trust caller, not whatever the model returned
    instruments.apply_tick_rounding(proposal, instruments.load_config())
    proposal["risk_reward"]   = _compute_rr(proposal)

    # Sanity-check the prices against feed.db's latest reference. Catches
    # hallucinated proposals where entry/stop/target are dozens of percent
    # off the instrument's actual range (e.g., MES at 5300, model emits 7400).
    is_valid, reason = proposal_sanity.sanity_check(proposal)

    record = {
        "instrument":       instrument,
        "screenshot_path":  None,
        "screenshot_filename": None,
        "proposal":         proposal,
        "raw_response":     raw,
        "duration_s":       round(time.monotonic() - started, 2),
        "model_duration_s": round(model_duration_s, 2),
        "model":            MODEL,
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
        "[headless] proposal stored: %s %s direction=%s confidence=%.2f",
        instrument, period, proposal.get("direction"), proposal.get("confidence", 0))
    return persisted


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


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> tuple[dict, str, float]:
    """Returns (parsed_proposal, raw_response_str, model_duration_s).

    num_ctx bumped to 8192 — default 2048 truncates our ~2k-token prompt
    when format=json adds grammar overhead, causing HTTP 500. qwen2.5vl
    supports up to 32k; 8k is comfortable headroom without bloating VRAM.
    """
    logger.info("[headless] POST %s (model=%s, prompt=%d chars)",
                OLLAMA_URL, MODEL, len(prompt))
    resp = requests.post(OLLAMA_URL, timeout=TIMEOUT, json={
        "model":   MODEL,
        "prompt":  prompt,
        "format":  "json",
        "stream":  False,
        "options": {"num_ctx": 8192},
    })
    resp.raise_for_status()
    body = resp.json()
    raw  = body["response"]
    duration_s = body.get("total_duration", 0) / 1e9
    return _parse_json(raw), raw, duration_s


def _parse_json(raw: str) -> dict:
    """Defensive parse — qwen sometimes wraps in fences even with format=json."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m: return json.loads(m.group(1))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m: return json.loads(m.group(0))
        raise


def _compute_rr(p: dict) -> float:
    if p.get("direction") == "flat":
        return 0.0
    try:
        e = float(p["entry"]); s = float(p["stop"]); t = float(p["target"])
        risk = abs(e - s)
        return round(abs(t - e) / risk, 2) if risk else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0
