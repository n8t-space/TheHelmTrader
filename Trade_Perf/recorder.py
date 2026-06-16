"""Recorder: pull executions from NT8's SQLite db into our local trades.db.

NT8 stores execution records in:
    %USERPROFILE%\\Documents\\NinjaTrader 8\\db\\NinjaTrader.sqlite

We open it read-only and copy fills into our denormalized 'fills' table.
Idempotent: re-running re-fetches only NT8 Executions.Id rows we haven't
already ingested (INSERT OR IGNORE on primary key).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import logging.handlers
import signal
import sqlite3
import sys
import time
from pathlib import Path


# ---- paths ----
HOME = Path.home()
NT8_DB = HOME / "Documents" / "NinjaTrader 8" / "db" / "NinjaTrader.sqlite"
LOCAL_DB = Path(__file__).parent / "trades.db"


# ---- NT8 enum decode (tentative -- verify as more data arrives) ----
ORDER_ACTION = {0: "Buy", 1: "Sell", 2: "SellShort", 3: "BuyToCover"}
ORDER_TYPE = {0: "Limit", 1: "Market", 2: "MIT", 3: "StopLimit", 4: "StopMarket"}
MARKET_POSITION = {0: "Flat", 1: "Long", 2: "Short"}

# .NET DateTime.Ticks: 100-ns intervals since 0001-01-01 00:00:00 UTC
DOTNET_EPOCH = datetime.datetime(1, 1, 1, tzinfo=datetime.UTC)


def decode_ticks(ticks: int) -> datetime.datetime:
    return DOTNET_EPOCH + datetime.timedelta(microseconds=ticks // 10)


def expiry_to_contract(expiry_int: int | None) -> str:
    # NT8 stores futures expiry as int YYYYMM (or YYYYMMDD) -> render 'MMMYY'
    # e.g. 202606 -> 'JUN26'. Returns "" (bare master symbol) for missing or
    # IMPLAUSIBLE expiries: a rolled contract occasionally comes in from NT8
    # with garbage like Expiry=199211, which used to render "MCL NOV92".
    # Futures expiries are near-dated, so anything outside a sane year window
    # is bad NT8 data -> fall back to the bare master symbol.
    if not expiry_int:
        return ""
    s = str(expiry_int)
    if len(s) < 6:
        return ""
    try:
        year = int(s[0:4])
        month = int(s[4:6])
    except ValueError:
        return ""
    this_year = datetime.datetime.now(datetime.timezone.utc).year
    if not (this_year - 1 <= year <= this_year + 6) or not (1 <= month <= 12):
        return ""
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return f"{months[month - 1]}{s[2:4]}"


# ---- local schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY,    -- mirrors NT8 Executions.Id
    execution_id      TEXT,
    order_id          TEXT,
    time_utc          TEXT NOT NULL,          -- ISO 8601 with millis, UTC
    account_name      TEXT,
    account_fcm       TEXT,
    symbol            TEXT,                   -- 'CL JUN26'
    master_symbol     TEXT,                   -- 'CL'
    point_value       REAL,
    tick_size         REAL,
    strategy_name     TEXT,
    strategy_class    TEXT,
    strategy_template TEXT,                   -- ATM template name ('40 for 400'), distinct from generic strategy_name
    order_name        TEXT,                   -- 'Entry' / 'Stop1' / 'Target1' / 'Close'
    order_action      TEXT,
    order_action_code INTEGER,
    order_type        TEXT,
    order_type_code   INTEGER,
    market_position   TEXT,                   -- pre-fill state, decoded
    is_entry          INTEGER,
    is_exit           INTEGER,
    qty               INTEGER,
    price             REAL,
    commission        REAL,
    fee               REAL,
    rate              REAL,
    position          INTEGER,                -- running net pos (account)
    position_strategy INTEGER,
    ingested_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_time            ON fills(time_utc);
CREATE INDEX IF NOT EXISTS idx_fills_account_symbol  ON fills(account_name, symbol);
CREATE INDEX IF NOT EXISTS idx_fills_strategy        ON fills(strategy_name);
CREATE INDEX IF NOT EXISTS idx_fills_order_id        ON fills(order_id);
"""


# Pulls a flat row per execution. LEFT JOIN throughout so missing dimensions
# don't cause us to silently drop fills.
FETCH_QUERY = """
SELECT
    e.Id               AS id,
    e.ExecutionId      AS execution_id,
    e.OrderId          AS order_id,
    e.Time             AS time_ticks,
    a.Name             AS account_name,
    a.Fcm              AS account_fcm,
    mi.Name            AS master_symbol_name,
    i.Expiry           AS expiry_int,
    mi.PointValue      AS point_value,
    mi.TickSize        AS tick_size,
    s.Name             AS strategy_name,
    s.Classname        AS strategy_class,
    s.Template         AS strategy_template,
    e.Name             AS order_name,
    o.OrderAction      AS order_action_code,
    o.OrderType        AS order_type_code,
    e.MarketPosition   AS market_position_code,
    e.IsEntry          AS is_entry,
    e.IsExit           AS is_exit,
    e.Quantity         AS qty,
    e.Price            AS price,
    e.Commission       AS commission,
    e.Fee              AS fee,
    e.Rate             AS rate,
    e.Position         AS position,
    e.PositionStrategy AS position_strategy
FROM Executions e
LEFT JOIN Accounts          a  ON a.Id  = e.Account
LEFT JOIN Instruments       i  ON i.Id  = e.Instrument
LEFT JOIN MasterInstruments mi ON mi.Id = i.MasterInstrument
LEFT JOIN Orders            o  ON o.OrderId = e.OrderId AND o.Account = e.Account
LEFT JOIN Strategy2Execution se ON se.Execution = e.Id
LEFT JOIN Strategies        s  ON s.Id = se.Strategy
WHERE e.Id > ?
ORDER BY e.Id
"""


def fetch_executions_since(nt8_db_path: Path, since_id: int) -> list[dict]:
    uri = f"file:{nt8_db_path}?mode=ro"
    out: list[dict] = []
    with sqlite3.connect(uri, uri=True) as src:
        src.row_factory = sqlite3.Row
        for r in src.execute(FETCH_QUERY, (since_id,)):
            d = dict(r)
            ticks = d.pop("time_ticks")
            iso = decode_ticks(ticks).isoformat(timespec="milliseconds")
            d["time_utc"] = iso.replace("+00:00", "Z")

            ms = d.pop("master_symbol_name") or ""
            ex = expiry_to_contract(d.pop("expiry_int"))
            d["master_symbol"] = ms
            d["symbol"] = f"{ms} {ex}".strip() if ex else ms

            d["order_action"] = ORDER_ACTION.get(d["order_action_code"]) or str(d.get("order_action_code"))
            d["order_type"] = ORDER_TYPE.get(d["order_type_code"]) or str(d.get("order_type_code"))
            mp_code = d.pop("market_position_code")
            d["market_position"] = MARKET_POSITION.get(mp_code) or str(mp_code)
            out.append(d)
    return out


def init_local_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    _migrate_add_strategy_template(path, NT8_DB)


def _migrate_add_strategy_template(local_db: Path, nt8_db: Path) -> None:
    """One-shot migration: add the strategy_template column if missing and
    backfill it from NT's Strategies.Template for every fill we already have.

    Idempotent. Pre-migration, all ATM fills had strategy_name='AtmStrategy'
    (the generic class label) and the actual template name was lost. This
    re-pulls Template by execution id from NT's live SQLite db.
    """
    with sqlite3.connect(local_db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(fills)")}
        needs_alter = "strategy_template" not in cols
        if needs_alter:
            conn.execute("ALTER TABLE fills ADD COLUMN strategy_template TEXT")
            logging.info("migration: added fills.strategy_template column")
        # Find rows missing template
        missing_ids = [r[0] for r in conn.execute(
            "SELECT id FROM fills WHERE strategy_template IS NULL"
        )]
    if not missing_ids:
        return

    if not nt8_db.exists():
        logging.warning("migration: NT db not found at %s -- skipping backfill", nt8_db)
        return

    nt_uri = f"file:{nt8_db}?mode=ro"
    try:
        with sqlite3.connect(nt_uri, uri=True) as src:
            template_by_id = dict(src.execute("""
                SELECT e.Id, s.Template
                FROM Executions e
                LEFT JOIN Strategy2Execution se ON se.Execution = e.Id
                LEFT JOIN Strategies s ON s.Id = se.Strategy
            """).fetchall())
    except sqlite3.Error as e:
        logging.warning("migration: could not read NT Strategies (%s); skipping", e)
        return

    updates = [(template_by_id.get(fid), fid) for fid in missing_ids
               if template_by_id.get(fid)]
    if not updates:
        return
    with sqlite3.connect(local_db) as conn:
        conn.executemany(
            "UPDATE fills SET strategy_template = ? WHERE id = ?", updates,
        )
        conn.commit()
    logging.info("migration: backfilled strategy_template on %d fill(s)", len(updates))


def upsert_fills(path: Path, rows: list[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    cols = list(rows[0].keys()) + ["ingested_at"]
    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT OR IGNORE INTO fills ({','.join(cols)}) VALUES ({placeholders})"
    inserted = 0
    with sqlite3.connect(path) as conn:
        for r in rows:
            r["ingested_at"] = now
            cur = conn.execute(sql, [r[c] for c in cols])
            inserted += cur.rowcount
    return inserted, len(rows) - inserted


def get_max_id(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        (max_id,) = conn.execute("SELECT COALESCE(MAX(id), 0) FROM fills").fetchone()
        return int(max_id)


def ingest_once() -> tuple[int, int, int]:
    init_local_db(LOCAL_DB)
    since = get_max_id(LOCAL_DB)
    rows = fetch_executions_since(NT8_DB, since)
    inserted, dupes = upsert_fills(LOCAL_DB, rows)
    return since, inserted, dupes


_stop_requested = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _stop_requested
    _stop_requested = True
    logging.info("stop signal received; exiting after current tick")


def _interruptible_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop_requested and time.monotonic() < end:
        time.sleep(min(0.5, max(0.0, end - time.monotonic())))


def watch(interval: float) -> int:
    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)
    logging.info("watching NT8 db every %.1fs (db=%s -> %s)", interval, NT8_DB, LOCAL_DB)
    consecutive_errors = 0
    while not _stop_requested:
        try:
            since, inserted, _ = ingest_once()
            if inserted:
                logging.info("ingested %d new fill(s) past id=%d", inserted, since)
            else:
                logging.debug("tick ok, no new fills since id=%d", since)
            consecutive_errors = 0
            sleep_for = interval
        except Exception:
            consecutive_errors += 1
            logging.exception("tick failed (%d consecutive)", consecutive_errors)
            sleep_for = min(interval * (2 ** min(consecutive_errors, 4)), 60.0)
        _interruptible_sleep(sleep_for)
    logging.info("watcher stopped cleanly")
    return 0


def setup_logging(log_path: Path | None, verbose: bool, to_stdout: bool) -> None:
    handlers: list[logging.Handler] = []
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if log_path is not None:
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        handlers.append(fh)
    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        handlers.append(sh)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="NT8 trade recorder")
    p.add_argument("--watch", action="store_true",
                   help="run continuously, polling NT8's db on an interval")
    p.add_argument("--interval", type=float, default=5.0,
                   help="polling interval in seconds (default 5)")
    p.add_argument("--log-file", default=str(Path(__file__).parent / "recorder.log"),
                   help="path to log file (use 'none' to disable)")
    p.add_argument("--no-stdout", action="store_true",
                   help="suppress stdout logging (useful when launched via pythonw)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv[1:])

    log_path = None if args.log_file.lower() == "none" else Path(args.log_file)
    setup_logging(log_path, args.verbose, to_stdout=not args.no_stdout)

    if not NT8_DB.exists():
        logging.error("NT8 db not found at %s", NT8_DB)
        return 1

    if args.watch:
        return watch(args.interval)

    since, inserted, dupes = ingest_once()
    logging.info("one-shot: since_id=%d inserted=%d dupes=%d", since, inserted, dupes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
