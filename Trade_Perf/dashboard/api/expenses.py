"""Business expense ledger.

A standalone, operator-authored expense tracker for the trading business -- eval
fees, resets, funded-activation fees, data/platform subscriptions, hardware,
education, professional services, etc. Each row carries an entity tag
(personal | llc) so the LLC's books split out from personal spend, and an
optional account link so eval cost can be netted against that account's payout
for per-eval ROI.

Stored in its own SQLite file (``expenses.db``) -- separate from trades.db
(recorder-owned) and journal.db. Pure operator input; no coupling to fills.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/expenses", tags=["expenses"])
logger = logging.getLogger(__name__)

# Own store, beside trades.db / journal.db at the project root.
DB_PATH = Path(__file__).resolve().parents[2] / "expenses.db"

# Business-expense categories (Schedule-C-flavored, futures-prop specific).
CATEGORIES = [
    "eval_fee", "reset_fee", "funded_activation", "platform_data",
    "software_subscription", "hardware", "education", "broker_fees",
    "professional_services", "payout_fee", "office", "other",
]
ENTITIES = ["personal", "llc"]


class Expense(BaseModel):
    id: int | None = None
    date: str = ""                       # YYYY-MM-DD (payment date)
    category: str = "other"
    amount: float = Field(default=0.0, ge=0.0)
    entity: str = "llc"                  # personal | llc
    vendor: str = ""
    account: str = ""                    # optional NT account id link
    recurring: bool = False              # monthly subscription flag (manual entry)
    deductible: bool = True              # business-deductible
    note: str = ""
    created_at: str = ""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL DEFAULT '',
            category    TEXT    NOT NULL DEFAULT 'other',
            amount      REAL    NOT NULL DEFAULT 0,
            entity      TEXT    NOT NULL DEFAULT 'llc',
            vendor      TEXT    NOT NULL DEFAULT '',
            account     TEXT    NOT NULL DEFAULT '',
            recurring   INTEGER NOT NULL DEFAULT 0,
            deductible  INTEGER NOT NULL DEFAULT 1,
            note        TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    return c


def _row(r: sqlite3.Row) -> Expense:
    return Expense(
        id=r["id"], date=r["date"], category=r["category"], amount=r["amount"],
        entity=r["entity"], vendor=r["vendor"], account=r["account"],
        recurring=bool(r["recurring"]), deductible=bool(r["deductible"]),
        note=r["note"], created_at=r["created_at"],
    )


def _clean(e: Expense) -> Expense:
    e.category = e.category if e.category in CATEGORIES else "other"
    e.entity = e.entity if e.entity in ENTITIES else "llc"
    return e


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("")
def list_expenses(year: int | None = None, entity: str | None = None) -> dict[str, Any]:
    """All expenses (newest first), optionally filtered by year (date prefix)
    and entity. Returns the rows plus roll-ups for the filtered set."""
    with _conn() as c:
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM expenses ORDER BY date DESC, id DESC"
        ).fetchall()]

    if year is not None:
        rows = [e for e in rows if (e.date or "")[:4] == str(year)]
    if entity in ENTITIES:
        rows = [e for e in rows if e.entity == entity]

    total = round(sum(e.amount for e in rows), 2)
    by_category: dict[str, float] = {}
    by_entity: dict[str, float] = {}
    by_account: dict[str, float] = {}
    by_year: dict[str, float] = {}
    for e in rows:
        by_category[e.category] = round(by_category.get(e.category, 0.0) + e.amount, 2)
        by_entity[e.entity] = round(by_entity.get(e.entity, 0.0) + e.amount, 2)
        if e.account:
            by_account[e.account] = round(by_account.get(e.account, 0.0) + e.amount, 2)
        yr = (e.date or "")[:4] or "?"
        by_year[yr] = round(by_year.get(yr, 0.0) + e.amount, 2)

    return {
        "expenses": [e.model_dump() for e in rows],
        "categories": CATEGORIES,
        "summary": {
            "total": total,
            "count": len(rows),
            "by_category": by_category,
            "by_entity": by_entity,
            "by_account": by_account,
            "by_year": dict(sorted(by_year.items(), reverse=True)),
        },
    }


@router.post("")
def create_expense(body: Expense) -> dict[str, Any]:
    e = _clean(body)
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO expenses
               (date, category, amount, entity, vendor, account, recurring,
                deductible, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.date, e.category, e.amount, e.entity, e.vendor, e.account,
             int(e.recurring), int(e.deductible), e.note, _now_z()),
        )
        c.commit()
        r = c.execute("SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row(r).model_dump()


@router.put("/{expense_id}")
def update_expense(expense_id: int, body: Expense) -> dict[str, Any]:
    e = _clean(body)
    with _conn() as c:
        if c.execute("SELECT 1 FROM expenses WHERE id = ?", (expense_id,)).fetchone() is None:
            raise HTTPException(404, "expense not found")
        c.execute(
            """UPDATE expenses SET
               date=?, category=?, amount=?, entity=?, vendor=?, account=?,
               recurring=?, deductible=?, note=? WHERE id=?""",
            (e.date, e.category, e.amount, e.entity, e.vendor, e.account,
             int(e.recurring), int(e.deductible), e.note, expense_id),
        )
        c.commit()
        r = c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    return _row(r).model_dump()


@router.delete("/{expense_id}", status_code=204)
def delete_expense(expense_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        c.commit()
