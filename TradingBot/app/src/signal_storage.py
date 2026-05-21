"""Append trade proposals + journal/outcome updates to a JSONL log.

`append_signal` writes the initial record and stamps the timestamp.
`append_update` writes a partial record (same timestamp) — readers merge latest-wins.

Records carry a ``schema_version`` field (added 2026-05-09). Records
without one are treated as v0 (legacy). Bump SCHEMA_VERSION whenever
the on-disk shape changes in a way readers built against the old shape
can't handle.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


DEFAULT_POSITION_SIZE = 1.0


def append_signal(jsonl_path: Path, record: dict[str, Any]) -> dict:
    """Append one signal record. Adds timestamp + schema_version.
    Defaults `position_size` to 1 contract when not supplied so realized
    P&L and per-signal W/L tally without the user needing to set it.
    Returns the enriched record."""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    enriched = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "position_size": DEFAULT_POSITION_SIZE,
        **record,
    }
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(enriched, default=str) + "\n")
    logger.info("Logged signal to %s (timestamp=%s)", jsonl_path, enriched["timestamp"])
    return enriched


def append_update(jsonl_path: Path, timestamp: str, **fields: Any) -> None:
    """Append an update record (e.g. journal or outcome edit) with same timestamp."""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    update = {
        "timestamp": timestamp,
        "type": "update",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **fields,
    }
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(update) + "\n")
    logger.info("Logged update for %s: %s", timestamp, list(fields))


MERGEABLE_FIELDS = (
    "journal",
    "outcome",
    "deleted",
    "position_size",
    "outcome_suggestion",
    "outcome_suggestion_dismissed",
    "entry_triggered",
    "entry_hit_ts",
    # Per-leg fills for multi-bracket ATM scale-outs. Top-level (not inside
    # outcome) so the auto-resolver can publish legs without overwriting a
    # user-edited aggregate outcome, and vice versa. Old records simply
    # lack this field; metrics calc falls back to single-outcome math.
    "legs",
)


def load_all(jsonl_path: Path) -> dict[str, dict]:
    """Read the JSONL and merge update records 'latest wins'. Keyed by timestamp.

    Logs (once) if any record's schema_version exceeds what this code
    knows about — a forward-incompat warning rather than a hard fail,
    since the JSONL is append-only and we still want partial reads.
    """
    signals: dict[str, dict] = {}
    if not jsonl_path.exists():
        return signals
    forward_incompat_warned = False
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ver = rec.get("schema_version", 0)
            if ver > SCHEMA_VERSION and not forward_incompat_warned:
                logger.warning(
                    "signals.jsonl record uses schema_version=%d but this code "
                    "expects <= %d; forward-incompat fields may be ignored.",
                    ver, SCHEMA_VERSION)
                forward_incompat_warned = True
            ts = rec.get("timestamp")
            if not ts:
                continue
            if ts not in signals:
                signals[ts] = rec
            else:
                for key in MERGEABLE_FIELDS:
                    if key in rec:
                        signals[ts][key] = rec[key]
    return signals
