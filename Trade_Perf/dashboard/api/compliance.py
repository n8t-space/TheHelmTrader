"""Microscalping-compliance tracker for prop-firm eval accounts.

Many futures eval/funding programs cap how much of an account's result may
come from ultra-short scalps. The rule tracked here:

  - At most 50% of GROSS profit may come from trades held < 10 seconds.
  - At most 50% of trades may be held < 10 seconds.

All-time, per account. Pure calc over derived round-trips (each carries
``duration_seconds`` + ``gross_pnl``). The P&L ratio is taken over gross
PROFIT (sum of winning trades' gross P&L), since a "percent of profit" only
has meaning on the positive side -- losses don't dilute a profit cap.

ESTIMATE -- firms define the window/threshold slightly differently; verify
against your program's exact terms.
"""
from __future__ import annotations

from typing import Iterable

SCALP_SECONDS = 10.0
MAX_PCT = 50.0


def microscalp_by_account(
    trades: list[dict],
    *,
    eval_accounts: Iterable[str] | None = None,
) -> dict:
    """Per-account microscalping ratios + a compliant flag.

    ``eval_accounts`` flags which accounts the rule actually binds (prop-firm
    evals); every account is still measured so the operator sees the same
    ratio on live/sim accounts.
    """
    evals = set(eval_accounts or ())

    per: dict[str, dict] = {}
    for t in trades:
        acct = t.get("account") or "?"
        a = per.setdefault(acct, {
            "account": acct,
            "trades": 0,
            "scalp_trades": 0,
            "gross_profit": 0.0,
            "scalp_gross_profit": 0.0,
        })
        dur = float(t.get("duration_seconds") or 0.0)
        gross = float(t.get("gross_pnl") or 0.0)
        is_scalp = dur < SCALP_SECONDS

        a["trades"] += 1
        if is_scalp:
            a["scalp_trades"] += 1
        if gross > 0:
            a["gross_profit"] += gross
            if is_scalp:
                a["scalp_gross_profit"] += gross

    accounts = []
    for a in per.values():
        trade_pct = (a["scalp_trades"] / a["trades"] * 100.0) if a["trades"] else 0.0
        pnl_pct = (a["scalp_gross_profit"] / a["gross_profit"] * 100.0) if a["gross_profit"] > 0 else 0.0
        accounts.append({
            "account":            a["account"],
            "is_eval":            a["account"] in evals,
            "trades":             a["trades"],
            "scalp_trades":       a["scalp_trades"],
            "scalp_trade_pct":    round(trade_pct, 1),
            "gross_profit":       round(a["gross_profit"], 2),
            "scalp_gross_profit": round(a["scalp_gross_profit"], 2),
            "scalp_pnl_pct":      round(pnl_pct, 1),
            "compliant":          trade_pct <= MAX_PCT and pnl_pct <= MAX_PCT,
        })

    # Evals first (the rule binds them), then worst offenders by the higher of
    # the two ratios so a breach is at the top.
    accounts.sort(key=lambda x: (not x["is_eval"],
                                 -max(x["scalp_trade_pct"], x["scalp_pnl_pct"])))

    return {
        "scalp_seconds": SCALP_SECONDS,
        "max_pct": MAX_PCT,
        "accounts": accounts,
        "note": ("Prop-firm eval rule: at most 50% of gross profit AND at most "
                 "50% of trades may come from sub-10-second holds. All-time; "
                 "gross-profit basis. Firms vary -- verify against your "
                 "program's exact terms."),
    }
