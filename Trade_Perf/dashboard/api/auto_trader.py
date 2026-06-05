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
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from . import _tradebot_bridge as bridge
from . import settings as settings_mod
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

    # Contract cap is a gate, not a clamp: the ATM template owns the order size
    # (AtmStrategyCreate takes no qty), so refuse to arm an oversize template.
    qty = _template_qty(proposal)
    if qty > cfg.max_contracts_per_order:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ATM template '{proposal.get('atm_strategy')}' places {qty} contracts, "
            f"over the Auto-Trader max of {cfg.max_contracts_per_order}. Raise the cap "
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
# {account: {"balance": float, "at": iso}}
_last_balance: dict[str, dict] = {}


@router.get("/auto-trader/account")
def auto_trader_account() -> dict:
    """Single source of truth for the auto-trade account (Settings > Auto-Trader).
    HelmAutoTrader fetches this on start and uses it as its allowed account, so
    the strategy's 'Allowed account' and the dashboard's 'Setup > Account' can
    never drift. Also surfaces the balance floor + last-reported equity."""
    cfg = settings_mod.auto_trader_config()
    bal = _last_balance.get(cfg.account or "")
    return {
        "account":             cfg.account or "",
        "enabled":             cfg.enabled,
        "min_account_balance": cfg.min_account_balance,
        "balance":             bal["balance"] if bal else None,
        "balance_at":          bal["at"] if bal else None,
    }


class BalanceUpdate(BaseModel):
    account: str
    balance: float


@router.post("/auto-trader/balance")
def report_balance(update: BalanceUpdate) -> dict:
    """The NT8 strategy reports the account's live equity here each poll. FAIL-SAFE:
    if equity is at/below the configured floor (min_account_balance > 0), force the
    master switch OFF -- new entries stop and the queue empties. We do NOT flatten
    the open position; it keeps its own ATM stop. Requires manual re-enable."""
    _last_balance[update.account] = {"balance": update.balance, "at": _now_iso()}
    cfg = settings_mod.auto_trader_config()
    floor = cfg.min_account_balance
    # Only act on a plausible POSITIVE equity -- a 0/garbage reading (account not
    # ready) must never trip the kill-switch. NS already guards, but enforce here.
    tripped = (floor > 0 and update.balance > 0 and update.balance <= floor
               and update.account == cfg.account)
    if tripped and cfg.enabled:
        settings_mod.set_auto_trader_enabled(False)
        logger.warning("[auto-trader] BALANCE FLOOR hit: %s equity %.2f <= floor %.2f "
                       "-- auto-trading forced OFF (manual re-enable required)",
                       update.account, update.balance, floor)
    return {"ok": True, "floor": floor, "tripped": tripped,
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

    # Fail-safe: hold the queue while reported equity is at/below the balance
    # floor (the balance report also forces the master switch OFF; this covers
    # the window between reports).
    floor = cfg.min_account_balance
    bal = _last_balance.get(account)
    if floor > 0 and bal and 0 < bal["balance"] <= floor:
        return {"account": account, "count": 0, "signals": [], "held_balance_floor": bal["balance"]}

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
    open_instruments = {
        instruments.normalize_symbol(str((r.get("proposal") or {}).get("instrument") or ""))
        for r in raw.values()
        if not r.get("deleted")
        and (r.get("exec") or {}).get("state") in ("working", "filled")
        and ((r.get("exec") or {}).get("account") or r.get("arm_account")) == account
        and _trade_still_open(r)
    } - {""}
    if len(open_instruments) >= max(1, cfg.max_concurrent):
        logger.info("[auto-trader] queue held: %d open instrument(s) >= max_concurrent=%d",
                    len(open_instruments), cfg.max_concurrent)
        return {"account": account, "count": 0, "signals": [], "held_for_open": len(open_instruments)}

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
        # The ATM template fixes order size; never offer one over the cap.
        if _template_qty(proposal) > cfg.max_contracts_per_order:
            continue
        # One open trade per instrument: skip if this instrument is already live.
        instr = instruments.normalize_symbol(str(proposal.get("instrument") or ""))
        if instr in open_instruments:
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

        out.append({
            "ts": ts,
            "exec_tag": ex.get("exec_tag") or _exec_tag(ts),
            "instrument": instr,
            "direction": direction,
            "entry": proposal.get("entry"),
            "limit_price": proposal.get("entry"),
            "atm_strategy": proposal.get("atm_strategy"),
            "qty": _template_qty(proposal),   # TRUE template size (not a clamp)
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
