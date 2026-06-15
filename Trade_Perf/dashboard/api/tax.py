"""Estimated tax on realized futures P&L (IRC Section 1256, 60/40).

Pure calc over derived round-trip trades. Futures (incl. MES/MCL micros) are
Section 1256 contracts: net gains are taxed 60% at long-term, 40% at
short-term/ordinary rates regardless of holding period, on one Form 6781 that
nets all such positions. Per account we tax that account's own positive net;
the netted total is the actual liability (a losing account offsets a winning
one at the return level).

ESTIMATE ONLY -- ignores year-end mark-to-market of OPEN positions (the
derivation drops open positions anyway), loss carrybacks, and is not tax
advice. Rates come from settings.Tax (fractions).
"""
from __future__ import annotations

from datetime import datetime

from .settings import Tax
from .trading_day import trading_day_for_ts


def _trade_year(exit_time: str, tz: str | None) -> int | None:
    """Calendar (trading-day) year a trade's realization booked into. Uses the
    futures-aware day roll so a fill after the CT session close attributes to
    the correct trade date, matching the rest of the app."""
    day = trading_day_for_ts(exit_time, tz) or (exit_time[:10] if exit_time else None)
    if not day:
        return None
    try:
        return int(day[:4])
    except (ValueError, TypeError):
        return None


def estimate_by_account(
    trades: list[dict],
    cfg: Tax,
    *,
    year: int,
    tz: str | None = None,
) -> dict:
    """Per-account + netted estimated tax for the given calendar year.

    Each account's tax is on its own positive net (a losing account owes $0,
    its loss noted). The total tax is on the netted P&L across accounts -- the
    real 1256 liability, since gains and losses net on one return.
    """
    rate = cfg.blended_rate

    per: dict[str, dict] = {}
    for t in trades:
        if _trade_year(t.get("exit_time", ""), tz) != year:
            continue
        acct = t.get("account") or "?"
        a = per.setdefault(acct, {"account": acct, "realized_pnl": 0.0, "trades": 0})
        a["realized_pnl"] += float(t.get("net_pnl") or 0.0)
        a["trades"] += 1

    accounts = []
    total_net = 0.0
    for a in per.values():
        net = round(a["realized_pnl"], 2)
        total_net += net
        taxable = max(0.0, net)
        accounts.append({
            "account":        a["account"],
            "trades":         a["trades"],
            "realized_pnl":   net,
            "taxable_gain":   round(taxable, 2),
            "estimated_tax":  round(taxable * rate, 2),
        })
    accounts.sort(key=lambda x: -x["realized_pnl"])

    total_net = round(total_net, 2)
    total_taxable = max(0.0, total_net)
    return {
        "tax_year": year,
        "enabled": cfg.enabled,
        "rates": {
            "lt_rate":     cfg.lt_rate,
            "st_rate":     cfg.st_rate,
            "state_rate":  cfg.state_rate,
            "blended_rate": rate,
        },
        "accounts": accounts,
        "total": {
            "realized_pnl":  total_net,
            "taxable_gain":  round(total_taxable, 2),
            "estimated_tax": round(total_taxable * rate, 2),
        },
        "note": ("Section 1256 60/40 estimate on realized P&L. Excludes year-end "
                 "mark-to-market of open positions and loss carrybacks. Not tax advice."),
    }


def current_tax_year(tz: str | None = None) -> int:
    """The calendar year to estimate for (operator-local now)."""
    return datetime.now().year
