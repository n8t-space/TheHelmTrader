"""Section 1256 60/40 per-account tax estimate."""
from __future__ import annotations

from dashboard.api import tax as tax_mod
from dashboard.api.settings import Tax


def _t(account, net, exit_time="2026-03-15T15:00:00+00:00"):
    return {"account": account, "net_pnl": net, "exit_time": exit_time}


def test_default_blended_rate_is_26_8pct():
    assert Tax().blended_rate == 0.268  # 0.6*0.20 + 0.4*0.37


def test_state_rate_adds_on_top():
    assert Tax(state_rate=0.05).blended_rate == 0.318


def test_per_account_gain_taxed_loss_is_zero():
    cfg = Tax()
    trades = [_t("A", 1000.0), _t("A", 500.0), _t("B", -300.0)]
    r = tax_mod.estimate_by_account(trades, cfg, year=2026, tz=None)
    acct = {a["account"]: a for a in r["accounts"]}
    assert acct["A"]["realized_pnl"] == 1500.0
    assert acct["A"]["estimated_tax"] == 402.0          # 1500 * 0.268
    assert acct["B"]["taxable_gain"] == 0.0             # loss -> no tax
    assert acct["B"]["estimated_tax"] == 0.0


def test_total_nets_accounts_then_taxes():
    # A's gain and B's loss net on one Form 6781 -> tax the netted total.
    cfg = Tax()
    trades = [_t("A", 1500.0), _t("B", -300.0)]
    r = tax_mod.estimate_by_account(trades, cfg, year=2026, tz=None)
    assert r["total"]["realized_pnl"] == 1200.0
    assert r["total"]["estimated_tax"] == 321.6         # 1200 * 0.268


def test_only_target_year_included():
    cfg = Tax()
    trades = [_t("A", 1000.0, "2026-02-01T15:00:00+00:00"),
              _t("A", 9999.0, "2025-12-01T15:00:00+00:00")]   # prior year excluded
    r = tax_mod.estimate_by_account(trades, cfg, year=2026, tz=None)
    assert r["total"]["realized_pnl"] == 1000.0
    assert r["accounts"][0]["trades"] == 1
