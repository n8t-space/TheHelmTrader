"""Per-trade trading journal.

Manual reflections keyed to a derived round-trip trade. The key is the same
``{first_fill_id}-{last_fill_id}`` the dashboard already uses to identify a
trade row, so an entry survives re-derivation as long as those two NT8
execution fills still exist.

Stored in its own SQLite file (``journal.db``) -- deliberately separate from
``trades.db``, which the recorder owns and migrates on its own schedule (and
which the API opens read-only). The journal is operator-authored, read-write,
and has zero coupling to the fills schema.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/journal", tags=["journal"])

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "journal.db"

# Closed set so the UI dropdown and the API agree; "" = unset.
MOODS = ["", "calm", "focused", "anxious", "fomo", "frustrated",
         "greedy", "confident", "bored", "revenge"]


class Snapshot(BaseModel):
    """Auto-stamped trade facts captured at journaling time so an entry stands
    alone on the Journal page without re-joining to trades.db.

    ``atm`` is the ATM/strategy template the trade ran under (NT8 'strategies'
    field, e.g. '40 for 400'); ``exit_price`` is the qty-weighted exit fill
    price -- i.e. where the stop/target actually closed it."""
    symbol: str = ""
    account: str = ""
    direction: str = ""
    net_pnl: float = 0.0
    entry_time: str = ""
    exit_time: str = ""
    atm: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0


class JournalEntry(BaseModel):
    trade_key: str = ""
    notes: str = ""
    discipline: int | None = Field(default=None, ge=1, le=5)
    mood: str = ""
    tags: list[str] = []
    snapshot: Snapshot = Snapshot()
    updated_at: str = ""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_journal (
            trade_key   TEXT PRIMARY KEY,
            notes       TEXT    NOT NULL DEFAULT '',
            discipline  INTEGER,
            mood        TEXT    NOT NULL DEFAULT '',
            tags        TEXT    NOT NULL DEFAULT '[]',
            snapshot    TEXT    NOT NULL DEFAULT '{}',
            updated_at  TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    return c


def _row_to_entry(r: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        trade_key=r["trade_key"],
        notes=r["notes"],
        discipline=r["discipline"],
        mood=r["mood"],
        tags=json.loads(r["tags"] or "[]"),
        snapshot=Snapshot(**json.loads(r["snapshot"] or "{}")),
        updated_at=r["updated_at"],
    )


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("")
def list_entries() -> dict[str, Any]:
    """All journal entries, most-recently-edited first. Powers the Journal page
    and the trades-table 'has-entry' indicators."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trade_journal ORDER BY updated_at DESC"
        ).fetchall()
    return {"entries": [_row_to_entry(r).model_dump() for r in rows]}


@router.get("/entry-screenshots")
def entry_screenshots() -> dict[str, Any]:
    """Map ``trade_key`` -> entry screenshot filename for auto-entered trades
    that captured one. Resolved through the fill-linker: signals carry the shot
    on ``exec.entry_screenshot``, the linker pairs each signal to its real
    round-trip, and we re-key by the trade's ``{first_fill_id}-{last_fill_id}``.

    Declared BEFORE ``/{trade_key}`` so the literal path wins over the param.
    Heavy imports are lazy (db + signals + instruments) and a failure degrades
    to an empty map rather than 500-ing the Journal."""
    # Lazy: these pull in the trades-derivation + signals stack.
    from . import _tradebot_bridge as bridge
    from . import fill_linker
    from src import signal_storage  # type: ignore[import-not-found]

    out: dict[str, str] = {}
    try:
        links = fill_linker.build_links()
        sigs = signal_storage.load_all(bridge.SIGNALS_LOG)
    except Exception:
        logger.exception("entry-screenshots: linkage failed")
        return {"screenshots": out}

    for ts, link in links.items():
        shot = ((sigs.get(ts) or {}).get("exec") or {}).get("entry_screenshot")
        trade = link.get("trade") or {}
        ffid, lfid = trade.get("first_fill_id"), trade.get("last_fill_id")
        if shot and ffid is not None and lfid is not None:
            out[f"{ffid}-{lfid}"] = shot
    return {"screenshots": out}


@router.get("/{trade_key}")
def get_entry(trade_key: str) -> dict[str, Any]:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM trade_journal WHERE trade_key = ?", (trade_key,)
        ).fetchone()
    if r is None:
        raise HTTPException(404, "no journal entry for this trade")
    return _row_to_entry(r).model_dump()


@router.put("/{trade_key}")
def upsert_entry(trade_key: str, body: JournalEntry) -> dict[str, Any]:
    """Create or update the entry for ``trade_key`` (path is authoritative; the
    body's trade_key is ignored). Empty notes + no rating + no tags still saves
    a row so the snapshot persists; delete to remove entirely."""
    mood = body.mood if body.mood in MOODS else ""
    tags = sorted({t.strip() for t in body.tags if t.strip()})
    with _conn() as c:
        c.execute(
            """
            INSERT INTO trade_journal
                (trade_key, notes, discipline, mood, tags, snapshot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_key) DO UPDATE SET
                notes      = excluded.notes,
                discipline = excluded.discipline,
                mood       = excluded.mood,
                tags       = excluded.tags,
                snapshot   = excluded.snapshot,
                updated_at = excluded.updated_at
            """,
            (trade_key, body.notes, body.discipline, mood,
             json.dumps(tags), body.snapshot.model_dump_json(), _now_z()),
        )
        c.commit()
        r = c.execute(
            "SELECT * FROM trade_journal WHERE trade_key = ?", (trade_key,)
        ).fetchone()
    return _row_to_entry(r).model_dump()


@router.delete("/{trade_key}", status_code=204)
def delete_entry(trade_key: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM trade_journal WHERE trade_key = ?", (trade_key,))
        c.commit()
