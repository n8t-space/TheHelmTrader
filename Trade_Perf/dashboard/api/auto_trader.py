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


@router.get("/auto-trader/account")
def auto_trader_account() -> dict:
    """Single source of truth for the auto-trade account (Settings > Auto-Trader).
    HelmAutoTrader fetches this on start and uses it as its allowed account, so
    the strategy's 'Allowed account' and the dashboard's 'Setup > Account' can
    never drift. Empty string when unset."""
    cfg = settings_mod.auto_trader_config()
    return {"account": cfg.account or "", "enabled": cfg.enabled}


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

    raw = signal_storage.load_all(bridge.SIGNALS_LOG)
    now = datetime.now()
    window = timedelta(minutes=cfg.entry_window_minutes)
    enabled_at = _parse_local(cfg.enabled_at)

    # Serialize execution: never offer a new entry while a position is still
    # open or in-flight on this account. "Open" = exec working/filled with no
    # terminal outcome yet (close is detected via the outcome resolver, so a
    # stale 'working' exec whose trade already resolved does NOT deadlock the
    # queue). Honors max_concurrent; set it to 1 for strict one-at-a-time. This
    # cap was previously never enforced at the queue, so the strategy could
    # stack multiple entries.
    open_now = sum(
        1 for r in raw.values()
        if not r.get("deleted")
        and (r.get("exec") or {}).get("state") in ("working", "filled")
        and ((r.get("exec") or {}).get("account") or r.get("arm_account")) == account
        and (r.get("outcome") or {}).get("result") in (None, "", "pending")
    )
    if open_now >= max(1, cfg.max_concurrent):
        logger.info("[auto-trader] queue held: %d open trade(s) >= max_concurrent=%d "
                    "-- waiting for current to close", open_now, cfg.max_concurrent)
        return {"account": account, "count": 0, "signals": [], "held_for_open": open_now}

    out: list[dict] = []
    for ts, rec in raw.items():
        if rec.get("deleted"):
            continue
        proposal = rec.get("proposal") or {}
        direction = (proposal.get("direction") or "").lower()
        if direction not in ("long", "short"):
            continue
        # The ATM template fixes order size; never offer one over the cap.
        if _template_qty(proposal) > cfg.max_contracts_per_order:
            continue

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
            "instrument": instruments.normalize_symbol(str(proposal.get("instrument") or "")),
            "direction": direction,
            "entry": proposal.get("entry"),
            "limit_price": proposal.get("entry"),
            "atm_strategy": proposal.get("atm_strategy"),
            "qty": _template_qty(proposal),   # TRUE template size (not a clamp)
        })
    out.sort(key=lambda r: r["ts"])
    return {"account": account, "count": len(out), "signals": out}


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
    signal_storage.append_update(bridge.SIGNALS_LOG, timestamp, **fields)
    logger.info("[auto-trader] EXEC %s -> %s%s", timestamp, update.state,
                " (dry-run)" if update.dry_run else "")
    return {"timestamp": timestamp, "exec": exec_obj}
