"""SQLite store for live market feed (bars + ticks) from the NinjaScript publisher.

Owns a single module-level connection to ``feed.db`` (WAL mode) protected by a
lock. Writes are dedup-on-insert: bars upsert on ``(instrument, period, ts)``,
ticks ignore-on-insert on ``(instrument, ts_ms, price)``.

FastAPI endpoints should wrap calls in ``asyncio.to_thread`` to avoid blocking
the event loop.
"""
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "feed.db"

# Bump when the on-disk shape of bars/ticks/auto_analysis_config changes
# in a way that breaks readers built against the old shape. The startup
# check below raises if it sees a HIGHER version than this code knows
# about (forward-incompat). Lower versions are migrated forward in
# init_schema() if/when migrations are added.
SCHEMA_VERSION = 1

_BARS_DDL = """
CREATE TABLE IF NOT EXISTS bars (
    instrument TEXT    NOT NULL,
    period     TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL,
    v INTEGER,
    PRIMARY KEY (instrument, period, ts)
)
"""

_TICKS_DDL = """
CREATE TABLE IF NOT EXISTS ticks (
    instrument TEXT    NOT NULL,
    ts_ms      INTEGER NOT NULL,
    price      REAL    NOT NULL,
    volume     INTEGER NOT NULL,
    PRIMARY KEY (instrument, ts_ms, price)
) WITHOUT ROWID
"""

_AUTO_ANALYSIS_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS auto_analysis_config (
    instrument TEXT    NOT NULL,
    period     TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (instrument, period)
)
"""

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


class SchemaVersionMismatchError(RuntimeError):
    """feed.db on disk is from a NEWER code version than this Python knows.
    Refusing to open is intentional — silently downgrading risks corrupt
    writes against a forward-incompatible schema."""


def init_schema() -> None:
    """Create tables if absent + check schema version. Idempotent."""
    with _lock:
        conn = _get_conn()
        conn.execute(_BARS_DDL)
        conn.execute(_TICKS_DDL)
        conn.execute(_AUTO_ANALYSIS_CONFIG_DDL)
        conn.execute(_SCHEMA_META_DDL)
        _validate_or_stamp_version(conn)
    logger.info("feed.db schema ready at %s (version=%d)", DB_PATH, SCHEMA_VERSION)


def _validate_or_stamp_version(conn: sqlite3.Connection) -> None:
    """Read schema version from schema_meta. Stamp on first init.
    Raise if stored version is HIGHER than this code expects."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        return
    stored = int(row[0])
    if stored > SCHEMA_VERSION:
        raise SchemaVersionMismatchError(
            f"feed.db has schema_version={stored} but this code expects "
            f"<= {SCHEMA_VERSION}. Update the bot before opening this DB."
        )
    # Older versions can be in-place migrated by adding stanzas here. None
    # needed at v1.


def get_schema_version() -> int:
    """Return the on-disk schema version (or 0 if uninitialized)."""
    with _lock:
        row = _get_conn().execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
    return int(row[0]) if row else 0


def insert_bar(
    instrument: str,
    period: str,
    ts: int,
    o: float,
    h: float,
    l: float,
    c: float,
    v: int,
) -> None:
    """Upsert one bar. PK conflict on (instrument, period, ts) replaces the row."""
    with _lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
            (instrument, period, ts, o, h, l, c, v),
        )


def insert_ticks(rows: list[tuple[str, int, float, int]]) -> None:
    """Bulk insert ticks. Dupes on (instrument, ts_ms, price) silently skipped."""
    if not rows:
        return
    with _lock:
        _get_conn().executemany(
            "INSERT OR IGNORE INTO ticks VALUES (?,?,?,?)",
            rows,
        )


# ---------------------------------------------------------------------------
# Auto-analysis config — small mutable table the dashboard manages.
# ---------------------------------------------------------------------------

def is_armed(instrument: str, period: str) -> bool:
    """True iff a row exists for (instrument, period) with enabled=1."""
    with _lock:
        row = _get_conn().execute(
            "SELECT 1 FROM auto_analysis_config "
            "WHERE instrument = ? AND period = ? AND enabled = 1",
            (instrument, period),
        ).fetchone()
    return row is not None


def list_config() -> list[dict]:
    """All config rows, sorted (instrument, period). Each row is a dict."""
    with _lock:
        rows = _get_conn().execute(
            "SELECT instrument, period, enabled FROM auto_analysis_config "
            "ORDER BY instrument, period"
        ).fetchall()
    return [
        {"instrument": i, "period": p, "enabled": bool(e)}
        for (i, p, e) in rows
    ]


def prune(retention_days: int = 7,
          protected_ts_seconds: int | None = None) -> dict:
    """Delete bars + ticks older than the retention cutoff.

    Cutoff = min(now − retention_days, protected_ts_seconds). The protected
    timestamp is the entry time of the oldest unresolved trade — keeping data
    older than the default cutoff so the outcome resolver can still walk it.

    Returns a dict with the cutoff (unix s) and deleted-row counts. Idempotent
    — running it twice in a row deletes zero the second time.
    """
    cutoff_s = int(time.time()) - retention_days * 86_400
    if protected_ts_seconds is not None and protected_ts_seconds < cutoff_s:
        cutoff_s = protected_ts_seconds
    cutoff_ms = cutoff_s * 1000
    with _lock:
        conn = _get_conn()
        bars_del  = conn.execute("DELETE FROM bars  WHERE ts    < ?", (cutoff_s,)).rowcount
        ticks_del = conn.execute("DELETE FROM ticks WHERE ts_ms < ?", (cutoff_ms,)).rowcount
    logger.info("feed.db prune: cutoff=%s bars=%d ticks=%d", cutoff_s, bars_del, ticks_del)
    return {"cutoff_s": cutoff_s, "bars_deleted": bars_del, "ticks_deleted": ticks_del}


def replace_config(entries: list[dict]) -> None:
    """Replace the entire config table with the given entries. Atomic.

    Each entry: {"instrument": str, "period": str, "enabled": bool}.
    Caller is responsible for the 4-armed-entry cap (UI-side).
    """
    rows = [
        (e["instrument"], e["period"], 1 if e["enabled"] else 0)
        for e in entries
    ]
    with _lock:
        conn = _get_conn()
        # Single transaction so list_config never sees a half-replaced state.
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM auto_analysis_config")
            if rows:
                conn.executemany(
                    "INSERT INTO auto_analysis_config VALUES (?,?,?)",
                    rows,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
