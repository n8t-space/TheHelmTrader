"""Read-only access to the local trades.db produced by recorder.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).resolve().parents[2] / "trades.db"


def _norm_account(account: list[str] | str | None) -> list[str]:
    """Accept str, list[str], or None. Returns a list (possibly empty) so
    callers can build IN-clauses uniformly. Tolerates legacy single-value
    callers (home.py, trades.py via tradelib) and the new multi-select route."""
    if account is None:
        return []
    if isinstance(account, str):
        return [account] if account else []
    return [a for a in account if a]


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"trades.db not found at {DB_PATH}; run recorder.py first")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_fills(
    *,
    account: list[str] | str | None = None,
    symbol: str | None = None,
    strategy: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    args: list = []
    accts = _norm_account(account)
    if accts:
        where.append(f"account_name IN ({','.join('?' * len(accts))})")
        args.extend(accts)
    if symbol:
        where.append("(symbol = ? OR master_symbol = ?)")
        args += [symbol, symbol]
    if strategy:
        # Match either the ATM template (preferred) or the raw strategy_name
        # so filtering by '40 for 400' still works for ATM-driven fills.
        where.append("COALESCE(strategy_template, strategy_name) = ?")
        args.append(strategy)
    if date_from:
        where.append("time_utc >= ?")
        args.append(date_from)
    if date_to:
        where.append("time_utc < ?")
        args.append(date_to)
    sql = "SELECT * FROM fills"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def fetch_fills_for_derivation(
    *,
    account: list[str] | str | None = None,
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Time-ordered, no pagination -- used by trade-derivation walk."""
    where: list[str] = []
    args: list = []
    accts = _norm_account(account)
    if accts:
        where.append(f"account_name IN ({','.join('?' * len(accts))})")
        args.extend(accts)
    if symbol:
        where.append("(symbol = ? OR master_symbol = ?)")
        args += [symbol, symbol]
    if date_from:
        where.append("time_utc >= ?")
        args.append(date_from)
    if date_to:
        where.append("time_utc < ?")
        args.append(date_to)
    sql = "SELECT * FROM fills"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_utc ASC, id ASC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def list_dimensions() -> dict[str, list[str]]:
    with connect() as conn:
        accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT account_name FROM fills WHERE account_name IS NOT NULL ORDER BY account_name")]
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT master_symbol FROM fills WHERE master_symbol IS NOT NULL ORDER BY master_symbol")]
        # Prefer the ATM template name over the generic "AtmStrategy" class
        # label, so the filter dropdown shows distinct templates the user
        # actually configured ('40 for 400', etc.) rather than one bucket.
        strategies = [r[0] for r in conn.execute(
            """SELECT DISTINCT COALESCE(strategy_template, strategy_name) AS s
               FROM fills WHERE COALESCE(strategy_template, strategy_name) IS NOT NULL
               ORDER BY s""")]
        (total_fills,) = conn.execute("SELECT COUNT(*) FROM fills").fetchone()
        first_time = conn.execute(
            "SELECT MIN(time_utc) FROM fills").fetchone()[0]
        last_time = conn.execute(
            "SELECT MAX(time_utc) FROM fills").fetchone()[0]
    return {
        "accounts": accounts,
        "symbols": symbols,
        "strategies": strategies,
        "total_fills": total_fills,
        "first_fill_time": first_time,
        "last_fill_time": last_time,
    }


__all__: Iterable[str] = (
    "DB_PATH", "connect", "fetch_fills", "fetch_fills_for_derivation", "list_dimensions",
)
