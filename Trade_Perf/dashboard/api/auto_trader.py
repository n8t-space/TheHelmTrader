"""Auto-Trader execution surface (Sim-only v1).

Per-signal manual arm: the user arms a proposal on the dashboard, the NT8
HelmAutoTrader strategy polls ``/api/exec/queue`` for its locked account, places
the ATM entry, and reports lifecycle back via ``/api/signals/{ts}/exec``.

Nothing here places orders -- it only manages the ``armed``/``exec`` state on the
signal record and gates it behind the Settings master switch + locked account.
All execution risk lives in the NT strategy; this module is the contract between
the dashboard and that strategy.

State machine (top-level ``exec`` object on the signal, merged latest-wins):
    armed -> working -> filled | cancelled | rejected
    armed -> disarmed            (user backs out before the strategy claims it)

``exec_tag = "helm_" + sanitized(signal_timestamp)`` is deterministic and unique
per signal; it is the NT orderId / atmStrategyId and the future exact linkage key.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge
from . import db
from . import settings as settings_mod
from . import trades as tradelib
from src import instruments, signal_storage  # type: ignore[import-not-found]  # via bridge

router = APIRouter(prefix="/api", tags=["auto-trader"])

logger = logging.getLogger(__name__)

EXEC_STATES = ("armed", "working", "filled", "cancelled", "rejected", "disarmed")
# States from which the user/strategy may no longer back out or re-arm.
TERMINAL_OR_LIVE = ("working", "filled")


def _now_iso() -> str:
    """Naive local ISO seconds -- matches the stamp style in signals.jsonl."""
    return datetime.now().isoformat(timespec="seconds")


def _exec_tag(ts: str) -> str:
    return "helm_" + re.sub(r"[^0-9A-Za-z]", "", ts)


def _load_signal(timestamp: str) -> dict:
    """Latest-wins record for one signal, or 404. Soft-deleted -> 404."""
    rec = signal_storage.load_all(bridge.SIGNALS_LOG).get(timestamp)
    if rec is None or rec.get("deleted"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"signal not found: {timestamp}")
    return rec


def _trade_still_open(rec: dict) -> bool:
    """Is this filled signal still holding a position?

    True while the outcome is unresolved (None/''/pending) OR any scale-out leg is
    still open -- a RUNNER trailing after TP1 set outcome='partial'. Keys on the
    signal's legs (maintained by the outcome-watcher, corrected to real fills by
    the auditor), NOT the raw NT8 fill ``position`` column -- that column is
    garbled for ATM reversal/scale-out fills (same-ms conflicting values), which
    would deadlock the queue on phantom open positions. A fully-resolved signal
    (all legs closed) frees its instrument so trading resumes."""
    if (rec.get("outcome") or {}).get("result") in (None, "", "pending"):
        return True
    # Legs are AUTHORITATIVE for whether a position is still held -- an outcome
    # can be set falsely (e.g. a 'stop' written while price never hit the stop and
    # the position is still running). Any leg the resolver couldn't close
    # (open / 'neither') means the trade is still live, regardless of outcome.
    # When the trade truly closes, the auditor backfills the legs from real fills,
    # which clears the lock.
    legs = rec.get("legs") or []
    return any((leg or {}).get("open") or (leg or {}).get("result") in (None, "neither")
               for leg in legs)


# A working/filled signal still counted "open" by the gate but with no activity
# for this long is a hung-trade candidate: an entry that never filled and never
# cancelled, or a position whose close never resolved. It blocks the queue.
HUNG_AGE_MIN = 30


def _hung_detail(rec: dict, now: datetime) -> dict | None:
    """Return a summary dict if this signal looks HUNG, else None.

    Hung = exec working/filled AND still open per the gate (`_trade_still_open`)
    AND no activity for >= HUNG_AGE_MIN minutes. A live, recently-active trade is
    NOT flagged. The detection is deliberately conservative; the operator clears
    via button -- they make the final call."""
    ex = rec.get("exec") or {}
    state = ex.get("state")
    if state not in ("working", "filled"):
        return None
    if not _trade_still_open(rec):
        return None  # already resolved -> not hung
    anchor = ex.get("filled_at") if state == "filled" else ex.get("working_at")
    try:
        age_min = (now - datetime.fromisoformat(anchor)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None
    if age_min < HUNG_AGE_MIN:
        return None
    proposal = rec.get("proposal") or {}
    return {
        "ts": rec.get("timestamp"),
        "instrument": proposal.get("instrument"),
        "direction": proposal.get("direction"),
        "state": state,
        "age_minutes": round(age_min),
        "outcome": (rec.get("outcome") or {}).get("result"),
        "account": ex.get("account") or rec.get("arm_account"),
        "fill_price": ex.get("fill_price"),
    }


def _template_qty(proposal: dict) -> int:
    """Contracts the ATM template will place. AtmStrategyCreate has NO quantity
    parameter -- size is fixed by the template's brackets -- so this is the TRUE
    order size, not something we can clamp. The per-order cap is therefore a
    GATE (refuse oversize), enforced at arm time, not a resize."""
    try:
        return max(1, int(proposal.get("atm_total_qty") or proposal.get("position_size") or 1))
    except (TypeError, ValueError):
        return 1


def _explicit_qty(proposal: dict) -> int | None:
    """An explicit qty the pipeline set on the proposal (position_size /
    atm_total_qty), or None if none. Used as the second tier of the ATM-less
    sizing cascade."""
    for key in ("position_size", "atm_total_qty"):
        v = proposal.get(key)
        if v is None:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _risk_sized_qty(cfg: settings_mod.AccountConfig, cash: float | None,
                    entry, stop, instrument: str) -> tuple[int | None, str]:
    """Item 3 risk-sizing formula. Returns (contracts, reason).

    contracts is None when an input is missing (cash in percent mode, stop
    distance, or tick_value) -- the caller then falls back to the Item-1B
    cascade. reason is a short log tag. Clamped to >= 1 and to the per-account
    max_contracts_per_instrument; the caller applies the hard per-order ceiling.

      percent mode: risk_dollars = cash * (value / 100)
      price mode:   risk_dollars = value (account-currency dollars)
      per_contract_risk = stop_distance_ticks * tick_value
      contracts = floor(risk_dollars / per_contract_risk), clamped [1, cap]
    """
    icfg = instruments.load_config()
    tick_size, _ = instruments.lookup_tick_size(instrument, icfg)
    point_value = instruments.lookup_point_value(instrument, icfg)
    if not tick_size or tick_size <= 0 or not point_value or point_value <= 0:
        return None, "no tick_value"
    tick_value = tick_size * point_value

    try:
        entry_f = float(entry)
        stop_f = float(stop)
    except (TypeError, ValueError):
        return None, "no stop"
    stop_distance_ticks = round(abs(entry_f - stop_f) / tick_size)
    if stop_distance_ticks <= 0:
        return None, "zero stop distance"

    mode = cfg.risk_per_trade_mode
    value = cfg.risk_per_trade_value
    if value <= 0:
        return None, "risk per trade not set"
    if mode == "percent":
        if cash is None or cash <= 0:
            return None, "no cash for percent sizing"
        risk_dollars = cash * (value / 100.0)
    else:  # price
        risk_dollars = value
    if risk_dollars <= 0:
        return None, "zero risk budget"

    per_contract_risk = stop_distance_ticks * tick_value
    if per_contract_risk <= 0:
        return None, "zero per-contract risk"
    contracts = int(math.floor(risk_dollars / per_contract_risk))
    contracts = max(1, min(contracts, cfg.max_contracts_per_instrument))
    return contracts, (f"risk-sized {mode} value={value} cash={cash} "
                       f"ticks={stop_distance_ticks} -> {contracts}")


def _resolved_qty(proposal: dict, account: str) -> tuple[int, str]:
    """The TRUE order qty for a queued signal + a log reason.

    ATM-template signal: the template fixes size (atm_total_qty).
    ATM-less directional: the Item-1B cascade --
        1. Item-3 risk sizing (per-account cfg, or the GLOBAL default cfg for a
           Sim account per D6), when cash + stop distance + tick_value allow it;
        2. explicit proposal qty (position_size / atm_total_qty);
        3. auto_trader.default_qty (global fallback);
        4. hard default 1.
    The hard per-order ceiling is applied by the caller's gate, not here."""
    if str(proposal.get("atm_strategy") or "").strip():
        return _template_qty(proposal), "atm template qty"

    cfg = settings_mod.account_config(account)
    # Sim / unconfigured account -> use the GLOBAL default config for sizing (D6).
    # The global config has no base_cash basis, so percent-mode sizing has no cash
    # source and degrades to the explicit-qty / default_qty / 1 cascade below
    # (price mode still works since it needs no cash). Documented seam.
    if cfg is None:
        at = settings_mod.auto_trader_config()
        cfg = settings_mod.AccountConfig(
            risk_per_trade_value=0.0,  # global has no risk % -> sizing won't run
            max_contracts_per_instrument=getattr(at, "max_contracts_per_order", 2),
        )
    # Computed current cash (base_cash + realized since basis), NOT NetLiquidation.
    # None when no basis is set -> percent-mode sizing falls back (see below).
    cash = current_cash(account)
    sized, reason = _risk_sized_qty(
        cfg, cash, proposal.get("entry"), proposal.get("stop"),
        str(proposal.get("instrument") or ""))
    if sized is not None:
        return sized, reason
    explicit = _explicit_qty(proposal)
    if explicit is not None:
        return explicit, f"explicit qty ({reason} unavailable)"
    dq = getattr(settings_mod.auto_trader_config(), "default_qty", 1)
    logger.info("[auto-trader] sizing fallback for %s: %s -> default_qty=%d",
                account, reason, dq)
    return max(1, dq), f"default_qty ({reason} unavailable)"


def _is_expired(armed_at: str | None, window: timedelta, now: datetime) -> bool:
    if not armed_at:
        return False
    try:
        return (now - datetime.fromisoformat(armed_at)) > window
    except (ValueError, TypeError):
        return False


# Cancel a still-pending entry this long before the next assessment fires.
ASSESSMENT_CANCEL_LEAD_S = 60

_PERIOD_RE = re.compile(r"^\s*(\d+)\s*([mhdMHD])\s*$")


def _period_to_seconds(period: str | None) -> int | None:
    """'15m' -> 900, '1h' -> 3600, '1d' -> 86400. None if unparseable."""
    if not period:
        return None
    m = _PERIOD_RE.match(period)
    if not m:
        return None
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


def _assessment_expiry(rec: dict) -> datetime | None:
    """When a still-pending signal must cancel: one lead-time before the NEXT
    assessment for its instrument (bar_ts + one period - lead). The next
    auto-analysis on this period will issue a fresh read, so a limit that hasn't
    filled by then is stale -- clear it just before, not after. Returns local
    naive datetime, or None for signals without period/bar info (manual snapshots
    -> the entry window applies instead)."""
    secs = _period_to_seconds(rec.get("headless_period"))
    bar_ts = rec.get("headless_bar_ts")
    if not secs or not isinstance(bar_ts, (int, float)):
        return None
    return datetime.fromtimestamp(int(bar_ts) + secs - ASSESSMENT_CANCEL_LEAD_S)


def _parse_local(s: str | None) -> datetime | None:
    """Parse a naive-local ISO stamp (signal timestamp / enabled_at)."""
    try:
        return datetime.fromisoformat(s)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Arm / disarm (dashboard -> signal)
# ---------------------------------------------------------------------------

@router.post("/signals/{timestamp}/arm")
def arm_signal(timestamp: str) -> dict:
    """Arm a proposal for execution on the configured locked account.

    Gated by the master switch + a set account. Refuses flat/invalid signals and
    anything already working/filled. Re-arming an already-armed signal is a
    no-op (idempotent)."""
    # Arming is staging intent -- it does NOT require the master switch to be on.
    # It only needs a configured account (so arm_account is meaningful and the
    # right strategy instance can claim it). Execution is separately gated by the
    # "enable auto trading" switch, which the strategy obeys via /exec/queue.
    cfg = settings_mod.auto_trader_config()
    if not cfg.account:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No Auto-Trader account configured (Settings -> Auto-Trader). Set one before arming.",
        )
    rec = _load_signal(timestamp)
    proposal = rec.get("proposal") or {}
    if (proposal.get("direction") or "").lower() not in ("long", "short"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot arm a flat/invalid signal.")

    # Contract cap is a gate, not a clamp. For an ATM template the template owns
    # the order size (AtmStrategyCreate takes no qty) -> refuse oversize. For an
    # ATM-less directional trade the qty is risk-sized (Item 3) and already
    # clamped to the per-account contract cap; we still gate on the effective
    # per-order ceiling (per-account else global) so an over-ceiling resolved qty
    # is refused at arm and never offered.
    gr = settings_mod.effective_guardrails(cfg.account)
    ceiling = gr.max_contracts_per_instrument
    qty, qty_reason = _resolved_qty(proposal, cfg.account)
    if qty > ceiling:
        is_atm = bool(str(proposal.get("atm_strategy") or "").strip())
        detail = (f"ATM template '{proposal.get('atm_strategy')}' places {qty} contracts"
                  if is_atm else f"risk sizing resolved {qty} contracts ({qty_reason})")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{detail}, over the Auto-Trader max of {ceiling}. Raise the cap "
            f"or pick a smaller template.",
        )

    cur = rec.get("exec") or {}
    if cur.get("state") in TERMINAL_OR_LIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Signal is already {cur.get('state')}; cannot re-arm.",
        )
    if cur.get("state") == "armed" and rec.get("arm_account") == cfg.account:
        return {"timestamp": timestamp, "exec": cur, "idempotent": True}

    exec_obj = {
        "state": "armed",
        "exec_tag": _exec_tag(timestamp),
        "account": cfg.account,
        "armed_at": _now_iso(),
    }
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp,
        armed=True, arm_account=cfg.account, exec=exec_obj,
    )
    logger.info("[auto-trader] ARMED %s for %s (tag=%s)", timestamp, cfg.account,
                exec_obj["exec_tag"])
    return {"timestamp": timestamp, "exec": exec_obj}


@router.post("/signals/{timestamp}/disarm")
def disarm_signal(timestamp: str) -> dict:
    """Back a signal out of the queue. Allowed only before the strategy claims
    it (state still 'armed'); once 'working'/'filled' it's too late here."""
    rec = _load_signal(timestamp)
    cur = rec.get("exec") or {}
    if cur.get("state") in TERMINAL_OR_LIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Signal is {cur.get('state')}; too late to disarm from the dashboard.",
        )
    exec_obj = {**cur, "state": "disarmed", "disarmed_at": _now_iso()}
    signal_storage.append_update(
        bridge.SIGNALS_LOG, timestamp, armed=False, exec=exec_obj,
    )
    logger.info("[auto-trader] DISARMED %s", timestamp)
    return {"timestamp": timestamp, "exec": exec_obj}


class EnableUpdate(BaseModel):
    enabled: bool


@router.post("/auto-trader/enable")
def set_enabled(update: EnableUpdate) -> dict:
    """Flip the live execution switch. OFF = armed signals stage but the strategy
    won't place them (the queue returns empty). Decoupled from arming so the user
    can pre-stage during a session and go live with one toggle."""
    cfg = settings_mod.set_auto_trader_enabled(update.enabled)
    logger.info("[auto-trader] execution %s", "ENABLED" if cfg.enabled else "DISABLED")
    return {"enabled": cfg.enabled, "account": cfg.account}


# Last account equity the NT8 strategy reported (runtime state, not persisted).
# {account: {"balance": float, "at": iso}}. RETAINED for back-compat on
# GET /api/auto-trader/account (the NS strategy still POSTs NetLiquidation), but
# it is NO LONGER the source for the Strategy-tab config card's cash -- that now
# comes from current_cash() (user-entered base_cash + realized since the basis).
# See report_balance for why we stopped relying on it for cash.
_last_balance: dict[str, dict] = {}


def _realized_since(account: str, basis_ts: str) -> float:
    """Cumulative realized P&L of `account`'s trades that CLOSED at/after
    `basis_ts` (UTC ISO with Z, matching trades.db exit_time). Reuses the existing
    /api/trades round-trip aggregation (db.fetch_fills_for_derivation +
    trades.derive_trades) -- does NOT re-implement P&L math. Returns 0.0 on any
    error or when trades.db is unavailable so cash degrades to base_cash, never
    raising into the config card."""
    if not account or not basis_ts:
        return 0.0
    try:
        # Fetch ALL of the account's fills (no date_from): a round-trip's ENTRY
        # fill can predate the basis while its EXIT is after, and derive_trades
        # needs both legs to build the trade. Push the lower bound into Python
        # below, on the trade's exit_time, NOT the raw fill time.
        fills = db.fetch_fills_for_derivation(account=[account])
        rows = tradelib.derive_trades(fills)
    except Exception as e:  # noqa: BLE001 -- never let a db hiccup break the card
        logger.warning("[auto-trader] realized-since failed for %s (%s)", account, e)
        return 0.0
    total = 0.0
    for t in rows:
        if t.get("account") != account:
            continue
        exit_t = t.get("exit_time") or ""
        if exit_t >= basis_ts:  # lexical compare is correct for UTC ISO-Z stamps
            total += float(t.get("net_pnl") or 0.0)
    return round(total, 2)


def current_cash(account: str) -> float | None:
    """Computed CURRENT cash for an account's config card (replaces the broker
    NetLiquidation pull): base_cash + cumulative realized P&L of trades that
    closed AFTER cash_basis_ts. Returns None when the account has no config OR no
    base_cash basis set (percent-mode sizing then has no cash source and falls
    back to the Item-1B cascade). With a basis but no trades since it, returns
    base_cash unchanged. This is the single cash source for risk sizing, the
    trailing-DD high-water mark, and the balance floor."""
    cfg = settings_mod.account_config(account)
    if cfg is None or cfg.base_cash <= 0 or not cfg.cash_basis_ts:
        return None
    return round(cfg.base_cash + _realized_since(account, cfg.cash_basis_ts), 2)


# Per-account equity HIGH-WATER MARK (Item 3 / D5). Now derived from the durable
# trades.db (current_cash) rather than transient NetLiquidation snapshots, so it
# survives a uvicorn restart: on each read we seed the HWM from the running
# high of current_cash and ratchet it up. In-memory still acts as the monotonic
# ceiling within a process; a restart re-seeds it from the next read (acceptable
# v1 -- the floor is derivable from trades since base_cash + realized is durable).
# {account: float}.
_equity_hwm: dict[str, float] = {}


def _update_hwm(account: str, cash: float | None) -> float | None:
    """Ratchet the per-account equity high-water mark to the latest current_cash.
    Returns the HWM (>= cash) or None when cash is unknown. Called wherever cash
    is computed (the live readout + the queue/arm path) so the HWM tracks the
    computed current cash, not NetLiquidation."""
    if cash is None or cash <= 0:
        return _equity_hwm.get(account)
    prev = _equity_hwm.get(account, cash)
    hwm = max(prev, cash)
    _equity_hwm[account] = hwm
    return hwm


def _trailing_dd_state(account: str) -> dict:
    """Trailing-DD readout for an account (Item 3 / D5): high-water mark,
    used (= hwm - current cash), the user-entered limit, and whether it is
    breached. CASH IS THE COMPUTED current cash (base_cash + realized since
    basis), NOT NetLiquidation. A breach forces the Auto-Trader OFF for that
    account (no auto-flatten), the same passive fail-safe as the balance floor."""
    cash = current_cash(account)
    hwm = _update_hwm(account, cash)
    cfg = settings_mod.account_config(account)
    limit = cfg.trailing_dd_limit if cfg else 0.0
    used = None
    breached = False
    if hwm is not None and cash is not None:
        used = round(hwm - cash, 2)
        if limit > 0 and used >= limit:
            breached = True
    return {
        "cash": cash,
        "high_water_mark": hwm,
        "trailing_dd_used": used,
        "trailing_dd_limit": limit,
        "dd_breached": breached,
    }


def _dd_breached(account: str) -> bool:
    """True when the account's server-computed trailing drawdown has breached
    its user-entered limit. Used to hold the queue (same as the balance floor)."""
    return bool(_trailing_dd_state(account).get("dd_breached"))


@router.get("/auto-trader/account")
def auto_trader_account(account: str | None = None) -> dict:
    """Single source of truth for the auto-trade account (Settings > Auto-Trader).
    HelmAutoTrader fetches this on start and uses it as its allowed account, so
    the strategy's 'Allowed account' and the dashboard's 'Setup > Account' can
    never drift. Also surfaces the balance floor + last-reported equity.

    Optional ?account= reports the per-account effective caps + last-reported
    equity for THAT account (Item 4), so the NS strategy can reflect per-account
    MaxContractsPerOrder / MaxConcurrent. With no ?account= it returns the
    locked account's view (unchanged contract for existing callers).

    NOTE: `balance`/`balance_at` here are the last NetLiquidation the NS strategy
    reported (_last_balance), kept for back-compat with the strategy. The config
    card's cash now comes from current_cash() (base_cash + realized since basis),
    NOT this field."""
    cfg = settings_mod.auto_trader_config()
    acct = account or cfg.account or ""
    bal = _last_balance.get(acct)
    gr = settings_mod.effective_guardrails(acct)
    return {
        "account":             cfg.account or "",
        "enabled":             cfg.enabled,
        # Global floor kept for back-compat; per-account effective floor below.
        "min_account_balance": cfg.min_account_balance,
        "balance":             bal["balance"] if bal else None,
        "balance_at":          bal["at"] if bal else None,
        # Per-account effective caps (Item 4). For the locked/queried account.
        "queried_account":          acct,
        "max_contracts_per_order":  gr.max_contracts_per_instrument,
        "max_concurrent":           gr.max_concurrent_per_instrument,
        "effective_balance_floor":  gr.stop_if_balance_below,
        "daily_loss_cutoff":        gr.max_daily_loss,
    }


@router.get("/account-configs/live")
def account_config_live(account: str) -> dict:
    """Live readout for one account's Strategy-tab config card (Item 3): the
    COMPUTED current cash (user-entered base_cash + realized P&L of trades that
    closed after the basis, from Trade Performance) + the server-computed
    trailing-DD state (HWM, used, limit, breach). cash is null when no base_cash
    basis is set. realized_since is the adjustment applied to base_cash so the UI
    can show the '= base + realized since <date>' hint."""
    cfg = settings_mod.account_config(account)
    base_cash = cfg.base_cash if cfg else 0.0
    basis_ts = cfg.cash_basis_ts if cfg else ""
    realized = _realized_since(account, basis_ts) if (base_cash > 0 and basis_ts) else 0.0
    state = _trailing_dd_state(account)
    return {
        "account":         account,
        "cash":            state.pop("cash"),
        "base_cash":       base_cash,
        "cash_basis_ts":   basis_ts,
        "realized_since":  realized,
        **state,
    }


class BalanceUpdate(BaseModel):
    account: str
    balance: float


@router.post("/auto-trader/balance")
def report_balance(update: BalanceUpdate) -> dict:
    """The NT8 strategy still POSTs its account NetLiquidation here each poll; we
    cache it in _last_balance only for back-compat on GET /api/auto-trader/account.
    It is NO LONGER the cash source for the config card, risk sizing, the
    trailing-DD high-water mark, or the balance floor -- all of those now use
    current_cash() (user-entered base_cash + realized P&L since the basis), which
    is correct even when no strategy is running on the account.

    FAIL-SAFE re-check on the COMPUTED current cash: if it is at/below the
    effective floor (per-account stop_if_balance_below, else global
    min_account_balance) OR the trailing drawdown breached the user-entered limit
    (Item 3 / D5), force the master switch OFF -- new entries stop and the queue
    empties. Open positions keep their own stop (no auto-flatten). The same
    re-check also runs in exec_queue, so the floor/DD are enforced even with no
    balance report coming in."""
    _last_balance[update.account] = {"balance": update.balance, "at": _now_iso()}
    return _enforce_fail_safe(update.account)


def _enforce_fail_safe(account: str) -> dict:
    """Trip the master switch OFF when the COMPUTED current cash is at/below the
    effective balance floor OR the trailing drawdown has breached its limit.
    Shared by report_balance and (read-only) the queue hold. Cash comes from
    current_cash(), so enforcement does not depend on a NetLiquidation report."""
    cfg = settings_mod.auto_trader_config()
    gr = settings_mod.effective_guardrails(account)
    floor = gr.stop_if_balance_below
    cash = current_cash(account)
    dd = _trailing_dd_state(account)
    dd_breached = bool(dd.get("dd_breached")) and account == cfg.account
    # Only act on a known, plausible POSITIVE cash -- an unset basis (None) or a
    # 0 must never trip the kill-switch.
    floor_tripped = (floor > 0 and cash is not None and cash > 0 and cash <= floor
                     and account == cfg.account)
    tripped = floor_tripped or dd_breached
    if tripped and cfg.enabled:
        settings_mod.set_auto_trader_enabled(False)
        if floor_tripped:
            logger.warning("[auto-trader] BALANCE FLOOR hit: %s cash %.2f <= floor %.2f "
                           "-- auto-trading forced OFF (manual re-enable required)",
                           account, cash, floor)
        if dd_breached:
            logger.warning("[auto-trader] TRAILING DD breached: %s used %.2f >= limit %.2f "
                           "(hwm %.2f) -- auto-trading forced OFF (manual re-enable required)",
                           account, dd.get("trailing_dd_used") or 0.0,
                           dd.get("trailing_dd_limit") or 0.0, dd.get("high_water_mark") or 0.0)
    return {"ok": True, "floor": floor, "cash": cash, "tripped": tripped,
            "floor_tripped": floor_tripped, "dd_breached": dd_breached,
            "enabled": settings_mod.auto_trader_config().enabled}


# ---------------------------------------------------------------------------
# Execution queue + lifecycle (NT strategy <-> signal)
# ---------------------------------------------------------------------------

@router.get("/exec/queue")
def exec_queue(account: str) -> dict:
    """Armed signals the strategy should act on for ``account``.

    Returns empty unless the master switch is on AND ``account`` matches the
    single locked account (server-side half of the account lock). Expired
    (past the entry window) armed signals are withheld -- the strategy cancels
    its own stale ATM entries; the queue just stops re-offering them."""
    cfg = settings_mod.auto_trader_config()
    if not cfg.enabled or not cfg.account or account != cfg.account:
        return {"account": account, "count": 0, "signals": []}

    # Automation blackout: pause auto-execution entirely during a configured
    # window (open positions keep their own ATM stop/target).
    blackout, label = settings_mod.in_blackout()
    if blackout:
        return {"account": account, "count": 0, "signals": [], "blackout": label}

    # Per-account effective guardrails (Item 4): per-account values override the
    # global defaults; a Sim/unconfigured account resolves to the globals (D6).
    gr = settings_mod.effective_guardrails(account)

    # Fail-safe: hold the queue while the COMPUTED current cash (base_cash +
    # realized since basis, NOT NetLiquidation) is at/below the effective balance
    # floor. Re-evaluates current_cash each poll so the floor is enforced even
    # with no NS balance report; also forces the master switch OFF.
    floor = gr.stop_if_balance_below
    cash = current_cash(account)
    if floor > 0 and cash is not None and 0 < cash <= floor:
        _enforce_fail_safe(account)
        return {"account": account, "count": 0, "signals": [], "held_balance_floor": cash}

    # Fail-safe: hold the queue on a trailing-DD breach (Item 3 / D5), same as
    # the balance floor (computed-cash based). Also forces the switch OFF.
    if _dd_breached(account):
        _enforce_fail_safe(account)
        return {"account": account, "count": 0, "signals": [], "held_trailing_dd": True}

    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    now = datetime.now()
    window = timedelta(minutes=cfg.entry_window_minutes)
    enabled_at = _parse_local(cfg.enabled_at)

    # --- Assessment-expiry sweep (BEFORE computing open instruments) ----------
    # A still-pending entry (working/armed/autonomous, unfilled) is stale by the
    # next assessment on its period -- mark it no_fill so it stops blocking its
    # instrument and is no longer offered. Must run before the open-instruments
    # set + ceiling check, else a working signal makes its OWN instrument "open"
    # and gets skipped before it can expire (and the ceiling could early-return).
    # The NS cancels the live entry via the expires_at it got at placement.
    for ts, rec in raw.items():
        if rec.get("deleted"):
            continue
        if (rec.get("exec") or {}).get("state") == "filled":
            continue
        if (rec.get("outcome") or {}).get("result") not in (None, "", "pending"):
            continue
        exp = _assessment_expiry(rec)
        if exp is not None and now >= exp:
            terminal = {"result": "no_fill",
                        "note": "assessment-expiry (cancel before next read)",
                        "closing_price": None}
            signal_storage.append_update(bridge.SIGNALS_LOG, ts,
                                         entry_triggered=False, outcome=terminal)
            rec["outcome"] = terminal           # so open_instruments excludes it now
            rec["entry_triggered"] = False
            logger.info("[auto-trader] assessment-expiry %s (%s) -> no_fill", ts,
                        instruments.normalize_symbol(str((rec.get("proposal") or {}).get("instrument") or "")))

    # Per-instrument serialization: don't offer a new entry for an instrument
    # that already has an open/in-flight trade -- but DO let other instruments
    # trade (one per instrument). "Open" = exec working/filled with no terminal
    # outcome yet (close is detected via the outcome resolver, so a stale
    # 'working' exec whose trade already resolved does NOT deadlock the queue).
    # max_concurrent is the overall ceiling across instruments. (Each NS
    # strategy instance trades one instrument and self-caps at 1, so this server
    # gate is what lets a second INSTRUMENT trade while the first is open.)
    # An instrument is open while it has a filled signal still holding a position
    # -- unresolved outcome OR a scale-out RUNNER whose leg is still open after
    # TP1 (outcome='partial'). Gating on leg state (not the garbled fill position
    # column) blocks stacking on a live runner without deadlocking on phantom
    # stale positions. A fully-resolved signal frees its instrument.
    # Count open trades PER instrument (Item 4): with
    # max_concurrent_per_instrument == 1 (default) this reproduces today's
    # one-open-per-instrument lock; > N allows up to N concurrent open trades on
    # that instrument for this account. open_instruments below is the set of
    # instruments AT their per-instrument cap (so a new entry is withheld).
    open_counts: dict[str, int] = {}
    for r in raw.values():
        if r.get("deleted"):
            continue
        if (r.get("exec") or {}).get("state") not in ("working", "filled"):
            continue
        if ((r.get("exec") or {}).get("account") or r.get("arm_account")) != account:
            continue
        if not _trade_still_open(r):
            continue
        sym = instruments.normalize_symbol(str((r.get("proposal") or {}).get("instrument") or ""))
        if not sym:
            continue
        open_counts[sym] = open_counts.get(sym, 0) + 1
    per_instr_cap = max(1, gr.max_concurrent_per_instrument)
    open_instruments = {sym for sym, n in open_counts.items() if n >= per_instr_cap}
    # max_concurrent_per_instrument also bounds the cross-instrument ceiling:
    # don't offer once the total open count across instruments hits the ceiling.
    total_open = sum(open_counts.values())
    cross_ceiling = max(1, gr.max_concurrent_per_instrument)
    if total_open >= cross_ceiling and per_instr_cap == 1:
        logger.info("[auto-trader] queue held: %d open instrument(s) >= ceiling=%d",
                    total_open, cross_ceiling)
        return {"account": account, "count": 0, "signals": [], "held_for_open": total_open}

    # Execution dedup -- at most ONE order per (instrument, bar). If a SIBLING
    # signal for the same instrument+bar was already acted on (placed/filled/
    # cancelled/etc), never place a second for that same bar -- even if the first
    # filled and closed fast, freeing the instrument. This guards against a
    # duplicate-bar signal that slips past the feed-side dispatch dedup. (The
    # supersede pass collapses concurrent armed siblings to the newest; this
    # catches the cross-poll case where a sibling already went out.)
    acted_bars = {
        (instruments.normalize_symbol(str((r.get("proposal") or {}).get("instrument") or "")),
         r.get("headless_bar_ts"))
        for r in raw.values()
        if not r.get("deleted")
        and (r.get("exec") or {}).get("state") in ("working", "filled", "cancelled", "rejected", "disarmed")
        and r.get("headless_bar_ts") is not None
    }

    out: list[dict] = []
    for ts, rec in raw.items():
        if rec.get("deleted"):
            continue
        proposal = rec.get("proposal") or {}
        direction = (proposal.get("direction") or "").lower()
        if direction not in ("long", "short"):
            continue
        # Already resolved or expired (superseded / no_fill / target / stop) ->
        # never offer it again.
        if (rec.get("outcome") or {}).get("result") not in (None, "", "pending"):
            continue
        # Resolve the TRUE order qty (ATM template size, or risk-sized for an
        # ATM-less trade) and never offer one over the effective per-order
        # ceiling -- it would be refused at arm anyway.
        resolved_qty, _qty_reason = _resolved_qty(proposal, account)
        if resolved_qty > gr.max_contracts_per_instrument:
            continue
        # Per-instrument concurrency: skip if this instrument is at its cap.
        instr = instruments.normalize_symbol(str(proposal.get("instrument") or ""))
        if instr in open_instruments:
            continue

        # One order per bar: a sibling signal for this exact instrument+bar was
        # already placed -> resolve this duplicate as no_fill, never place it.
        bar_ts = rec.get("headless_bar_ts")
        if bar_ts is not None and (instr, bar_ts) in acted_bars:
            signal_storage.append_update(
                bridge.SIGNALS_LOG, ts,
                entry_triggered=False,
                outcome={"result": "no_fill",
                         "note": "duplicate bar (an order was already placed this bar)",
                         "closing_price": None},
            )
            logger.info("[auto-trader] duplicate-bar skip %s (%s bar=%s)", ts, instr, bar_ts)
            continue

        # Time the strategy should cancel this entry if still unfilled (~1 min
        # before the next assessment), passed as expires_at below. Already-expired
        # signals were swept to no_fill above, so this is always in the future.
        exp = _assessment_expiry(rec)

        ex = rec.get("exec") or {}
        state = ex.get("state")
        # In-flight, finished, or explicitly opted out -> never (re)offer.
        if state in ("working", "filled", "cancelled", "rejected", "disarmed"):
            continue

        armed = state == "armed" and rec.get("arm_account") == account
        if armed:
            # Explicit manual arm: honor its own expiry window.
            if _is_expired(ex.get("armed_at"), window, now):
                continue
        else:
            # Autonomous: auto-trading is ON (checked above), so a qualifying
            # signal needs no manual arm. Gated to avoid surprises --
            #   * created at/after the OFF->ON enable moment (no backlog replay)
            #   * still fresh (within the entry window)
            created = _parse_local(ts)
            fresh = created is not None and (now - created) <= window
            after_enable = enabled_at is None or (created is not None and created >= enabled_at)
            if not (fresh and after_enable):
                continue

        # stop/target travel to the strategy for the ATM-less OCO path (Item 1B).
        # An ATM-template signal carries them too but the NS ATM path ignores
        # them (the template owns the bracket).
        def _numf(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out.append({
            "ts": ts,
            "exec_tag": ex.get("exec_tag") or _exec_tag(ts),
            "instrument": instr,
            "direction": direction,
            "entry": proposal.get("entry"),
            "limit_price": proposal.get("entry"),
            "atm_strategy": proposal.get("atm_strategy"),
            # TRUE order size: ATM template qty, or risk-sized for ATM-less.
            "qty": resolved_qty,
            "stop": _numf(proposal.get("stop")),
            "target": _numf(proposal.get("target")),
            # Unix seconds to cancel an unfilled entry (~1 min before the next
            # assessment). null -> strategy falls back to EntryWindowMinutes.
            "expires_at": exp.timestamp() if exp else None,
        })
    out.sort(key=lambda r: r["ts"])

    # One live signal per instrument. Keep only the NEWEST offerable per
    # instrument and expire the older same-instrument ones (mark no_fill
    # "superseded") -- so a queued entry that never triggered is cleared the
    # moment a fresher signal for that instrument exists. Net: queue <= 1 per
    # instrument, with built-in expiry as the next signal arrives.
    newest_by_instr: dict[str, dict] = {}
    for item in out:                       # ascending by ts -> last wins = newest
        newest_by_instr[item["instrument"]] = item
    live_ts = {item["ts"] for item in newest_by_instr.values()}
    for item in out:
        if item["ts"] in live_ts:
            continue
        signal_storage.append_update(
            bridge.SIGNALS_LOG, item["ts"],
            entry_triggered=False,
            outcome={"result": "no_fill", "note": "superseded by newer signal",
                     "closing_price": None},
        )
        logger.info("[auto-trader] expired superseded queue entry %s (%s)",
                    item["ts"], item["instrument"])

    signals = sorted(newest_by_instr.values(), key=lambda r: r["ts"])
    return {"account": account, "count": len(signals), "signals": signals}


@router.get("/auto-trader/hung")
def list_hung(account: str | None = None) -> dict:
    """Working/filled signals stuck 'open' with no activity for HUNG_AGE_MIN+ min
    -- entries that never filled/cancelled, or positions whose close never
    resolved. These block the per-instrument queue. Optionally scope to one
    account."""
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    now = datetime.now()
    hung: list[dict] = []
    for rec in raw.values():
        if rec.get("deleted"):
            continue
        d = _hung_detail(rec, now)
        if d and (account is None or d["account"] == account):
            hung.append(d)
    hung.sort(key=lambda h: h["ts"])
    return {"count": len(hung), "threshold_minutes": HUNG_AGE_MIN, "hung": hung}


class ClearHung(BaseModel):
    # Specific signals to clear; omit/empty -> clear every auto-detected hung one.
    timestamps: list[str] | None = None


@router.post("/auto-trader/clear-hung")
def clear_hung(body: ClearHung) -> dict:
    """Force a hung signal terminal so it stops blocking the queue: mark the
    outcome 'cleared', close any open/'neither' legs, and (for a never-filled
    working entry) flip entry_triggered off. The auditor can still later
    reconcile P&L from real fills -- clearing only unblocks the gate now."""
    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    now = datetime.now()
    requested = set(body.timestamps or [])
    cleared: list[str] = []
    for ts, rec in raw.items():
        if rec.get("deleted"):
            continue
        if requested:
            if ts not in requested or not _trade_still_open(rec):
                continue
        elif _hung_detail(rec, now) is None:
            continue
        legs = [
            {**leg,
             "open": False,
             "result": (leg.get("result") if leg.get("result") not in (None, "neither")
                        else "cleared")}
            for leg in (rec.get("legs") or []) if isinstance(leg, dict)
        ]
        fields: dict = {
            "outcome": {"result": "cleared",
                        "note": "manually cleared (hung trade)",
                        "closing_price": None,
                        "auto_confirmed": True},
        }
        if legs:
            fields["legs"] = legs
        if (rec.get("exec") or {}).get("state") == "working":
            fields["entry_triggered"] = False
        signal_storage.append_update(bridge.SIGNALS_LOG, ts, **fields)
        cleared.append(ts)
        logger.info("[auto-trader] cleared hung signal %s", ts)
    return {"cleared": len(cleared), "timestamps": sorted(cleared)}


class ExecUpdate(BaseModel):
    state: str = Field(..., pattern=f"^({'|'.join(s for s in EXEC_STATES if s != 'armed')})$")
    exec_tag: str | None = None
    fill_price: float | None = None
    fill_qty: float | None = None
    note: str | None = None
    dry_run: bool | None = None


_SAFE_SHOT_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _capture_entry_screenshot(rec: dict, exec_tag: str) -> str | None:
    """On an auto-entry fill, copy HelmFeed's latest chart screenshot for the
    signal's instrument to a stable ``entry_{exec_tag}.png`` so the per-trade
    Journal can show the chart at entry. Returns the saved filename, or None if
    the feature is off or no source screenshot is on disk yet.

    Best-effort: HelmFeed overwrites one ``auto_{instrument}_{period}.png`` per
    bar close, so this is the chart as of the most recent fed bar -- within a
    bar of the actual fill, not tick-exact."""
    proposal = rec.get("proposal") or {}
    instrument = str(proposal.get("instrument") or "")
    if not instrument:
        return None
    safe_i = _SAFE_SHOT_NAME.sub("_", instrument)
    shots_dir = bridge.SCREENSHOTS_DIR
    try:
        candidates = [
            p for p in shots_dir.glob("auto_*.png")
            if safe_i in p.name
        ]
    except OSError:
        return None
    if not candidates:
        return None
    src = max(candidates, key=lambda p: p.stat().st_mtime)
    dest_name = f"entry_{_SAFE_SHOT_NAME.sub('_', exec_tag)}.png"
    try:
        shutil.copyfile(src, shots_dir / dest_name)
    except OSError:
        logger.exception("[auto-trader] entry-screenshot copy failed for %s", exec_tag)
        return None
    logger.info("[auto-trader] captured entry screenshot %s <- %s", dest_name, src.name)
    return dest_name


@router.post("/signals/{timestamp}/exec")
def update_exec(timestamp: str, update: ExecUpdate) -> dict:
    """Strategy-driven lifecycle transition for an armed signal.

    Optimistic-concurrency claim guard (the dedup mechanism): a 'working' claim
    is rejected if the signal is already working/filled, so two poll cycles can
    never double-place. A filled signal is frozen. On 'filled' we coerce
    entry_triggered=True to satisfy the entry/outcome invariant."""
    rec = _load_signal(timestamp)
    cur = rec.get("exec") or {}
    cur_state = cur.get("state")

    if update.state == "working" and cur_state in TERMINAL_OR_LIVE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Signal already {cur_state}; claim rejected.")
    if cur_state == "filled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Signal already filled; frozen.")

    exec_obj = {
        **cur,
        "state": update.state,
        "exec_tag": update.exec_tag or cur.get("exec_tag") or _exec_tag(timestamp),
        "updated_at": _now_iso(),
    }
    if update.fill_price is not None:
        exec_obj["fill_price"] = update.fill_price
    if update.fill_qty is not None:
        exec_obj["fill_qty"] = update.fill_qty
    if update.note:
        exec_obj["note"] = update.note
    if update.dry_run is not None:
        exec_obj["dry_run"] = update.dry_run
    if update.state == "working":
        exec_obj["working_at"] = _now_iso()
    if update.state == "filled":
        exec_obj["filled_at"] = _now_iso()
        # Opt-in: stash the chart-at-entry for the Journal. Skip on dry-runs
        # (no real position) and never let a capture failure block the fill.
        if not exec_obj.get("dry_run") and \
                settings_mod.auto_trader_config().capture_entry_screenshot:
            shot = _capture_entry_screenshot(rec, exec_obj["exec_tag"])
            if shot:
                exec_obj["entry_screenshot"] = shot
    # The locked account is the only one that executes; stamp it so AUTONOMOUS
    # signals (never armed -> no account on exec) still carry the account for the
    # fill_linker exec-exact match.
    exec_obj["account"] = cur.get("account") or settings_mod.auto_trader_config().account

    fields: dict = {"exec": exec_obj}
    if update.state == "filled":
        fields["entry_triggered"] = True
    elif update.state == "cancelled":
        # An unfilled entry was cancelled (on the chart or via entry-window
        # expiry). Mark it a no-fill so it's excluded from realized P&L and the
        # outcome resolver stops walking it -- but keep the signal on the board
        # (no soft-delete). Don't clobber an outcome that's already final.
        fields["entry_triggered"] = False
        if not (rec.get("outcome") or {}).get("result"):
            fields["outcome"] = {
                "result": "no_fill",
                "note": exec_obj.get("note") or "order cancelled",
                "closing_price": None,
            }
    signal_storage.append_update(bridge.SIGNALS_LOG, timestamp, **fields)
    logger.info("[auto-trader] EXEC %s -> %s%s", timestamp, update.state,
                " (dry-run)" if update.dry_run else "")
    return {"timestamp": timestamp, "exec": exec_obj}
