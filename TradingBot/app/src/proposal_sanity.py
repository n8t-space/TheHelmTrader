"""Sanity-check LLM-generated trade proposals against current market price.

The vision LLM occasionally misreads a chart's price axis and emits a
proposal whose entry/stop/target are dozens of percent off the actual
instrument range (e.g., MES at 5300 but proposal claims entry=7406).
Those proposals are unresolvable, pollute the signal board, and cause the
outcome watcher to scan them every 30s forever.

This module compares a proposal's prices against the latest tick (or, if
no ticks, the latest bar close) for that instrument in ``feed.db``. If
any price differs by more than ``MAX_PRICE_DRIFT`` from the reference, the
proposal is flagged. Callers can then soft-delete the record at write
time and log a clear reason.

If ``feed.db`` has no data for the instrument, the check is a no-op: we
can't validate against an absent oracle. Once HelmFeed runs on every
traded chart, this gap closes naturally.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import instruments, runtime_config

logger = logging.getLogger(__name__)

# Generous threshold: 5% off the reference is already wider than any
# realistic intraday gap. The MES hallucination we caught was 39% off.
MAX_PRICE_DRIFT = 0.05

DATA_DIR     = Path(__file__).resolve().parent.parent / "data"
FEED_DB_PATH = DATA_DIR / "feed.db"


def sanity_check(proposal: dict,
                 require_atm: bool | None = None) -> tuple[bool, str | None]:
    """Return ``(is_valid, reason)``. ``flat`` proposals are always valid.

    A proposal fails when entry/stop/target differ from the latest reference
    price by more than ``MAX_PRICE_DRIFT``. Missing feed data = no opinion
    (returns valid).

    ``require_atm`` (Item 1A): when True a directional proposal with a blank
    ``atm_strategy`` is rejected (legacy behavior). When None the live
    ``runtime_config.require_atm_for_directional()`` flag is read (default
    False -> ATM optional). The price-drift validation runs regardless of ATM.
    """
    if proposal.get("direction") == "flat":
        return True, None

    if require_atm is None:
        require_atm = runtime_config.require_atm_for_directional()

    # ATM is OPTIONAL by default (Item 1A / D1): only reject a blank-ATM
    # directional proposal when the kill-switch is on. With it off the trade is
    # executed via the bare-LIMIT OCO path using the LLM's own stop/target, so a
    # blank strategy is no longer meaningless.
    if require_atm and not str(proposal.get("atm_strategy") or "").strip():
        return False, "directional proposal has no ATM strategy"

    instrument = proposal.get("instrument")
    if not instrument:
        return False, "missing instrument"

    inst_clean = instruments.normalize_symbol(instrument)
    ref_price = _latest_reference_price(inst_clean)
    if ref_price is None:
        # No feed data for this instrument; we can't validate. Let it through.
        return True, None

    for key in ("entry", "stop", "target"):
        raw = proposal.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if ref_price <= 0:
            continue
        drift = abs(v - ref_price) / ref_price
        if drift > MAX_PRICE_DRIFT:
            return False, (
                f"{key}={v} differs from current {inst_clean} ~{ref_price:g} "
                f"by {drift:.1%} (>{MAX_PRICE_DRIFT:.0%} threshold)"
            )

    return True, None


def _latest_reference_price(instrument: str) -> float | None:
    """Most current price in feed.db for the instrument. Prefer the latest
    tick; fall back to the latest bar close. Returns None if no data."""
    if not FEED_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{FEED_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT price FROM ticks WHERE instrument = ? "
            "ORDER BY ts_ms DESC LIMIT 1",
            (instrument,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT c FROM bars WHERE instrument = ? "
                "ORDER BY ts DESC LIMIT 1",
                (instrument,),
            ).fetchone()
        return float(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
