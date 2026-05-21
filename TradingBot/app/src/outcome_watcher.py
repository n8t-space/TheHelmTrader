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
    from . import entry_resolver, outcome_resolver, signal_storage
    from . import instruments as _instruments

    logger.info("[outcome-watcher] starting (interval=%ds)", CHECK_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            await asyncio.to_thread(
                _one_pass, signals_path,
                signal_storage, entry_resolver, outcome_resolver, _instruments,
            )
        except asyncio.CancelledError:
            logger.info("[outcome-watcher] shutdown")
            return
        except Exception:
            logger.exception("[outcome-watcher] iteration failed")


def _aggregate_leg_outcome(legs: list[dict]) -> str:
    """Collapse per-leg results to a single outcome label.

    All target -> 'target'; all initial-stop -> 'stop'; mix of target + anything
    -> 'partial'; otherwise -> 'partial' (legs that hit BE or trailed out
    aren't full stop-outs and aren't clean targets either).
    """
    results = [leg.get("result") for leg in legs]
    if all(r == "target" for r in results):
        return "target"
    if all(r == "stop" for r in results):
        return "stop"
    return "partial"


def _one_pass(signals_path, signal_storage, entry_resolver, outcome_resolver,
              instruments_mod) -> None:
    sigs = signal_storage.load_all(signals_path)

    # Identify candidates: not deleted, no final outcome, no PRIOR RESOLVER
    # pass on this signal (legacy LLM-reconciliation suggestions don't block
    # -- that feature was removed 2026-05-19), not flat, and has the price
    # triple we need to walk.
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
        # For multi-bracket scale-outs we may have written PARTIAL legs on a
        # prior pass while the runner kept trailing -- re-walk those each pass
        # until every leg closes. Bracket-aware signals therefore skip the
        # outcome_suggestion guard below.
        p = rec.get("proposal") or {}
        brackets = p.get("atm_brackets") or []
        has_brackets = bool(brackets)
        if not has_brackets:
            suggestion = rec.get("outcome_suggestion") or {}
            if suggestion.get("result") and suggestion.get("engine") == "resolver":
                # Already walked by the bar resolver; outcome write must have
                # failed previously. The earlier backfill handles those.
                continue
        else:
            existing_legs = rec.get("legs") or []
            if (len(existing_legs) == len(brackets)
                    and all((leg or {}).get("result") not in (None, "neither")
                            for leg in existing_legs)):
                # All legs already resolved -- nothing more to discover.
                continue
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

        # --- Step 1: was the entry actually touched? --------------------
        # Skip if we've already resolved this (entry_triggered is bool).
        # Otherwise call the entry resolver. Three outcomes:
        #   hit       -> mark entry_triggered=True + hit_ts
        #   no_entry  -> mark entry_triggered=False (and STOP -- no point
        #                checking stop/target on a trade that never opened)
        #   pending   -> leave alone, try next pass
        if rec.get("entry_triggered") is None:
            try:
                er = entry_resolver.resolve_entry(
                    instrument=inst_clean,
                    entry_ts=entry_ts,
                    entry=float(p["entry"]),
                )
            except Exception:
                logger.exception("[outcome-watcher] resolve_entry failed for %s", ts)
                er = None

            if er is not None:
                if er["state"] == "hit":
                    signal_storage.append_update(
                        signals_path, ts,
                        entry_triggered=True,
                        entry_hit_ts=er["hit_ts"],
                    )
                    rec["entry_triggered"] = True
                    logger.info(
                        "[outcome-watcher] entry HIT for %s (method=%s, ts_ms=%s)",
                        ts, er["method"], er["hit_ts"],
                    )
                elif er["state"] == "no_entry":
                    # Trade never opened -> outcome is no_fill by definition.
                    # Bundle the entry_triggered flip + outcome write so the
                    # signal lands in W/L tallies without manual touch-up.
                    signal_storage.append_update(
                        signals_path, ts,
                        entry_triggered=False,
                        outcome={"result": "no_fill",
                                 "note": "Auto: 4h window expired without entry touch",
                                 "closing_price": None,
                                 "auto_confirmed": True},
                    )
                    logger.info(
                        "[outcome-watcher] NO ENTRY for %s (4h window expired) -> outcome=no_fill",
                        ts)
                    # Trade never opened -- don't even try stop/target.
                    continue
                # state=="pending" -> fall through; entry may still get hit,
                # but it also could resolve stop/target on the same pass if
                # bars happen to span the entry already.

        # If entry_triggered=False, the trade never opened. Per the
        # not-entered rule, outcome MUST be no_fill -- normalize anything
        # else (legacy data or out-of-band edits) and skip stop/target.
        if rec.get("entry_triggered") is False:
            existing_outcome = (rec.get("outcome") or {}).get("result")
            if existing_outcome != "no_fill":
                signal_storage.append_update(
                    signals_path, ts,
                    outcome={"result": "no_fill",
                             "note": f"Auto-backfill: entry_triggered=False; "
                                     f"prior outcome={existing_outcome!r} normalized",
                             "closing_price": None,
                             "auto_confirmed": True},
                )
                logger.info(
                    "[outcome-watcher] normalized outcome for %s (was %r -> no_fill)",
                    ts, existing_outcome,
                )
            continue

        # --- Step 2: did stop or target get hit? ------------------------
        # Bracket-aware path: scale-out ATMs carry a per-bracket plan on the
        # proposal. Run the per-bracket state machine and write legs as they
        # resolve. Aggregate outcome is only written once every leg closes.
        brackets = p.get("atm_brackets") or []
        if brackets:
            try:
                tick_size, _ = instruments_mod.lookup_tick_size(
                    inst_full, instruments_mod.load_config())
            except Exception:
                logger.exception("[outcome-watcher] tick_size lookup failed for %s", ts)
                continue
            if not tick_size or tick_size <= 0:
                # No tick size -> can't translate tick distances to prices.
                # Fall through to the legacy single-outcome path.
                pass
            else:
                try:
                    legs = outcome_resolver.resolve_brackets(
                        instrument=inst_clean,
                        direction=p["direction"],
                        entry_ts=entry_ts,
                        entry_price=float(p["entry"]),
                        tick_size=float(tick_size),
                        brackets=brackets,
                    )
                except Exception:
                    logger.exception("[outcome-watcher] resolve_brackets failed for %s", ts)
                    continue
                # Skip if nothing has resolved at all (price still in the
                # initial range). Try again next pass.
                resolved_count = sum(1 for leg in legs if leg["result"] != "neither")
                if resolved_count == 0:
                    continue
                # Tag each leg with the resolver as the source so the UI can
                # distinguish auto-suggested legs from user-entered ones.
                tagged_legs = [{**dict(leg), "engine": "resolver"} for leg in legs]
                signal_storage.append_update(signals_path, ts, legs=tagged_legs)
                all_done = resolved_count == len(legs)
                if all_done:
                    agg = _aggregate_leg_outcome(legs)
                    # Audit suggestion + final outcome write, same shape as the
                    # single-bracket path so the rest of the dashboard is happy.
                    signal_storage.append_update(
                        signals_path, ts,
                        outcome_suggestion={
                            "result": agg,
                            "source_signal_ts": ts,
                            "engine": "resolver-brackets",
                        },
                        outcome={
                            "result": agg,
                            "note": f"Auto-resolved per-leg ({len(legs)} brackets)",
                            "closing_price": None,
                            "auto_confirmed": True,
                        },
                        entry_triggered=True,
                    )
                    logger.info(
                        "[outcome-watcher] BRACKETS RESOLVED %s for %s "
                        "(legs=%s)", agg, ts,
                        [(leg["result"], leg["exit_price"]) for leg in legs],
                    )
                else:
                    logger.info(
                        "[outcome-watcher] PARTIAL bracket progress for %s "
                        "(%d/%d legs closed)", ts, resolved_count, len(legs))
                continue  # bracket path handled; skip single-bracket fallback

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

        # Audit trail: how the resolver got here (hit_ts, hit_price, method).
        suggestion = {
            "result":           result["outcome"],
            "source_signal_ts": ts,
            "hit_ts":           result["hit_ts"],
            "hit_price":        result["hit_price"],
            "method":           result["method"],
            "engine":           "resolver",
        }
        signal_storage.append_update(
            signals_path, ts, outcome_suggestion=suggestion)

        # Write the outcome directly for BOTH headless and manual signals.
        # The "Confirm" yellow-banner path was retired with the cross-signal
        # reconciliation feature on 2026-05-19; auto-resolution from feed.db
        # is deterministic, so trust it. User can still override via the
        # Signal Detail page if the bar walker got it wrong.
        #
        # Also stamp entry_triggered to match the outcome-vs-entry rule:
        # any non-no_fill outcome means the entry was hit (the walker
        # found a stop or target). Without this, signals where entry_resolver
        # returned "pending" but outcome_resolver saw a hit in the same pass
        # would still display "no entry" in the table.
        is_no_fill = result["outcome"] == "no_fill"
        signal_storage.append_update(
            signals_path, ts,
            outcome={
                "result":         result["outcome"],
                "note":           f"Auto-resolved from feed.db "
                                  f"via {result['method']} at price "
                                  f"{result['hit_price']}",
                "closing_price":  result["hit_price"],
                "auto_confirmed": True,
            },
            entry_triggered=(not is_no_fill),
        )
        logger.info(
            "[outcome-watcher] AUTO-CONFIRMED %s for %s (method=%s, price=%s)",
            result["outcome"], ts, result["method"], result["hit_price"],
        )
