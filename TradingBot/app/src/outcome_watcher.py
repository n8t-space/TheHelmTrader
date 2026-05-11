"""Periodic outcome resolver — the "Independent Confirmation" feature.

Walks ``signals.jsonl`` every ``CHECK_INTERVAL_S`` seconds, finds unresolved
trades, and asks ``outcome_resolver`` whether their target/stop has been
touched per the bars+ticks in ``feed.db``. When the resolver returns a
verdict, the watcher writes an ``outcome_suggestion`` update to the signal.
The dashboard surfaces that suggestion as a yellow-banner confirm/reject
prompt — the user always has final say.

Distinct from ``pipeline.py``'s LLM reconciliation: that runs only on
manual Ctrl+Shift+F snip, uses the LLM, and is bounded to 3 most-recent
open trades. This watcher runs continuously, uses no LLM (much cheaper +
more deterministic), and walks every unresolved signal that has data in
``feed.db``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 30   # poll cadence; cheap, but no need to be tighter
RESULTS_PER_PASS = 50   # safety cap so a flood of new signals can't lock the loop


def _signal_ts_to_unix_s(ts_iso: str) -> int | None:
    """Convert a signal's ISO timestamp (e.g., '2026-05-10T17:17:36') to
    unix seconds. Returns None for malformed values rather than raising."""
    try:
        return int(datetime.fromisoformat(ts_iso).timestamp())
    except (ValueError, TypeError):
        return None


async def watcher_loop(signals_path: Path) -> None:
    """Background task entry point. Indefinite loop with per-iteration
    try/except so a bad signal record can't crash the watcher."""
    # Local imports keep main.py's lifespan startup tolerant of partial
    # codebases (e.g., during a refactor where outcome_resolver is missing).
    from . import outcome_resolver, signal_storage
    from . import instruments as _instruments

    logger.info("[outcome-watcher] starting (interval=%ds)", CHECK_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            await asyncio.to_thread(
                _one_pass, signals_path,
                signal_storage, outcome_resolver, _instruments,
            )
        except asyncio.CancelledError:
            logger.info("[outcome-watcher] shutdown")
            return
        except Exception:
            logger.exception("[outcome-watcher] iteration failed")


def _one_pass(signals_path, signal_storage, outcome_resolver, instruments_mod) -> None:
    sigs = signal_storage.load_all(signals_path)

    # Identify candidates: not deleted, no final outcome, no prior suggestion
    # (whether from us or from the LLM reconciliation pass), not flat, and
    # has the price triple we need to walk.
    candidates: list[tuple[str, dict, dict]] = []
    for ts, rec in sigs.items():
        if rec.get("deleted"):
            continue
        outcome = rec.get("outcome") or {}
        # 'pending' means the user explicitly hasn't decided yet -- still
        # eligible for auto-resolution. Any other non-empty result means
        # the trade is closed (or the user marked it not_watched / other).
        if outcome.get("result") and outcome.get("result") != "pending":
            continue
        suggestion = rec.get("outcome_suggestion") or {}
        if suggestion.get("result"):
            continue
        if rec.get("outcome_suggestion_dismissed"):
            continue
        p = rec.get("proposal") or {}
        if p.get("direction") in (None, "flat"):
            continue
        if not all(k in p for k in ("instrument", "direction", "entry", "stop", "target")):
            continue
        candidates.append((ts, rec, p))

    if not candidates:
        return

    candidates = candidates[-RESULTS_PER_PASS:]  # cap; most recent first if flood

    logger.info("[outcome-watcher] scanning %d unresolved signal(s)", len(candidates))

    for ts, rec, p in candidates:
        entry_ts = _signal_ts_to_unix_s(ts)
        if entry_ts is None:
            continue

        # signals.jsonl carries full contract names ("MES JUN26"); feed.db
        # carries stripped form ("MES"). Use the project's normalizer.
        inst_full = str(p.get("instrument") or "")
        inst_clean = instruments_mod.normalize_symbol(inst_full)

        try:
            result = outcome_resolver.resolve_outcome(
                instrument=inst_clean,
                direction=p["direction"],
                entry_ts=entry_ts,
                target=float(p["target"]),
                stop=float(p["stop"]),
            )
        except Exception:
            logger.exception("[outcome-watcher] resolve_outcome failed for %s", ts)
            continue

        if result["outcome"] == "neither":
            # No determination yet — either no data covers the window
            # (signal predates HelmFeed deployment) or price hasn't moved
            # enough to hit either level. Try again next pass.
            continue

        # Persist as outcome_suggestion — the user confirms via dashboard.
        signal_storage.append_update(
            signals_path, ts,
            outcome_suggestion={
                "result":           result["outcome"],
                "source_signal_ts": ts,
                "hit_ts":           result["hit_ts"],
                "hit_price":        result["hit_price"],
                "method":           result["method"],
                "engine":           "resolver",
            },
        )
        logger.info(
            "[outcome-watcher] suggested %s for %s (method=%s, price=%s)",
            result["outcome"], ts, result["method"], result["hit_price"],
        )
