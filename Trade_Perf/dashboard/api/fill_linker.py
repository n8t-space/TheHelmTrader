"""Link a signal proposal to the real NT8 round-trip trade that executed it.

No unique order tag exists today: orders are placed manually in NT's ATM panel,
so the bot never stamps an id on them. We therefore match heuristically on
instrument + direction + an entry-time window + entry-price proximity, score the
pairs 0..1, and assign one-to-one (greedy, best score first). Anything below the
confidence floor stays UNLINKED for user review rather than guessed -- same
honesty principle that retired the old LLM reconciliation pipeline.

Forward-compat: if a future account-scoped auto-exec path stamps a unique tag on
both the proposal and the fills, ``_exact_tag_score`` short-circuits to a perfect
match and the heuristic never runs. That hook is inert until such a tag exists.

The matched trade's real ``exit_fills`` carry the actual trailed-stop exit price,
which is the whole point: downstream metrics can then book realized P&L off the
real fill instead of the simulated bracket walk.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import _tradebot_bridge  # noqa: F401  -- side effect: puts TradingBot/app on sys.path
from . import db
from .trades import derive_trades

from src import instruments  # noqa: E402  (import depends on the bridge above)
from src import signal_storage  # noqa: E402

logger = logging.getLogger(__name__)

# Tunables (domain calls -- override if real fills say otherwise).
ENTRY_WINDOW_S = 4 * 3600    # a trade must open within 4h after the signal
CLOCK_SLACK_S = 120          # tolerate the order landing slightly before the log stamp
PRICE_TOL_TICKS = 8.0        # entry slippage budget; full credit at 0, zero at this
PRICE_HARD_TICKS = 24.0      # beyond this the prices disagree too much to be one trade
CONFIDENCE_FLOOR = 0.60      # below this -> leave unlinked


def _parse_utc(ts: str) -> datetime | None:
    """Parse a trades.db UTC stamp ('...Z' or '+00:00') to an aware datetime."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _signal_created_utc(rec: dict) -> datetime | None:
    """signals.jsonl stamps naive LOCAL time (datetime.now()). Localize via the
    machine tz and convert to UTC so it lines up with trades.db's UTC fills."""
    ts = rec.get("timestamp")
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach system-local tz
    return dt.astimezone(timezone.utc)


# When a signal was auto-executed we know its real fill time (exec.filled_at);
# the matching trade's entry fill lands within seconds. Tolerance covers
# reporting latency + clock skew between the dashboard stamp and NT's fill ts.
EXEC_MATCH_TOL_S = 300.0


def _tick_size_for(symbol: str, config: dict) -> float | None:
    ts, _ = instruments.lookup_tick_size(symbol, config)
    return ts if ts and ts > 0 else None


def _naive_local_to_utc(s: str | None) -> datetime | None:
    """Parse a dashboard-written naive-local ISO stamp (e.g. exec.filled_at) to UTC."""
    try:
        dt = datetime.fromisoformat(s)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _exact_tag_score(rec: dict, trade: dict) -> float | None:
    """Dormant deterministic hook. Returns 1.0 when both the proposal and the
    trade carry the same explicit exec tag, else None. No tag exists today, so
    this always returns None; wired in for the future account-scoped auto-exec
    path where the bot can stamp orders it placed itself."""
    proposal = rec.get("proposal") or {}
    sig_tag = proposal.get("exec_tag") or rec.get("exec_tag")
    trade_tag = trade.get("exec_tag")
    if sig_tag and trade_tag and sig_tag == trade_tag:
        return 1.0
    return None


def _score(rec: dict, trade: dict, config: dict) -> tuple[float, dict] | None:
    """Score one (signal, trade) pair. Returns (confidence, detail) or None if a
    hard filter rejects the pair."""
    exact = _exact_tag_score(rec, trade)
    if exact is not None:
        return exact, {"match": "exact_tag"}

    proposal = rec.get("proposal") or {}
    direction = (proposal.get("direction") or "").lower()
    if direction not in ("long", "short"):
        return None

    # --- hard filter: instrument ----------------------------------------
    sig_root = instruments.normalize_symbol(str(proposal.get("instrument") or ""))
    if not sig_root or sig_root != (trade.get("symbol") or ""):
        return None

    # --- hard filter: direction -----------------------------------------
    if direction != (trade.get("direction") or "").lower():
        return None

    # --- hard filter: entry-time window ---------------------------------
    created = _signal_created_utc(rec)
    t_entry = _parse_utc(trade.get("entry_time"))
    if created is None or t_entry is None:
        return None
    dt_s = (t_entry - created).total_seconds()
    if dt_s < -CLOCK_SLACK_S or dt_s > ENTRY_WINDOW_S:
        return None

    # --- hard filter: entry-price sanity --------------------------------
    tick = _tick_size_for(sig_root, config)
    try:
        sig_entry = float(proposal.get("entry"))
        trade_entry = float(trade.get("entry_price"))
    except (TypeError, ValueError):
        return None
    if tick is None:
        return None
    dticks = abs(trade_entry - sig_entry) / tick
    if dticks > PRICE_HARD_TICKS:
        return None

    # --- soft scoring ---------------------------------------------------
    price_closeness = max(0.0, 1.0 - dticks / PRICE_TOL_TICKS)
    time_closeness = max(0.0, 1.0 - max(dt_s, 0.0) / ENTRY_WINDOW_S)

    sig_atm = proposal.get("atm_strategy")
    template_match = bool(sig_atm) and sig_atm in (trade.get("strategies") or [])

    if sig_atm:
        confidence = 0.50 * price_closeness + 0.35 * (1.0 if template_match else 0.0) \
            + 0.15 * time_closeness
    else:
        # No ATM name to confirm against -> lean entirely on price + timing.
        confidence = 0.80 * price_closeness + 0.20 * time_closeness

    detail = {
        "match": "heuristic",
        "dticks": round(dticks, 2),
        "dt_seconds": round(dt_s, 1),
        "template_match": template_match,
        "price_closeness": round(price_closeness, 3),
        "time_closeness": round(time_closeness, 3),
    }
    return confidence, detail


def link_signals_to_trades(
    signals: list[dict],
    trades: list[dict],
    config: dict,
    *,
    floor: float = CONFIDENCE_FLOOR,
) -> dict[str, dict]:
    """Match signals to trades one-to-one, greedy by descending confidence.

    ``signals``: list of merged signal records (from signal_storage.load_all).
    ``trades``:  list of round-trip trades (from trades.derive_trades).
    Returns {signal_timestamp: {trade, confidence, detail}} for matches at or
    above ``floor``. Each signal and each trade is used at most once.
    """
    linked: dict[str, dict] = {}
    used_trades: set[int] = set()

    # --- Pass 1: exec-exact for auto-executed signals -------------------
    # A signal the auto-trader filled carries exec.exec_tag + exec.filled_at +
    # the locked account. That's a deterministic key: link it to the trade on
    # that account + instrument whose entry fill is closest to filled_at. No
    # price/heuristic scoring -- confidence 1.0.
    for rec in signals:
        if rec.get("deleted"):
            continue
        ts = rec.get("timestamp")
        ex = rec.get("exec") or {}
        if not ts or ex.get("state") != "filled" or not ex.get("exec_tag"):
            continue
        filled = _naive_local_to_utc(ex.get("filled_at"))
        if filled is None:
            continue  # no time anchor -> let the heuristic pass handle it
        acct = ex.get("account") or rec.get("arm_account")
        root = instruments.normalize_symbol(str((rec.get("proposal") or {}).get("instrument") or ""))
        best_ti: int | None = None
        best_dt: float | None = None
        for ti, trade in enumerate(trades):
            if ti in used_trades:
                continue
            if acct and trade.get("account") != acct:
                continue
            if root and (trade.get("symbol") or "") != root:
                continue
            te = _parse_utc(trade.get("entry_time"))
            if te is None:
                continue
            d = abs((te - filled).total_seconds())
            if d > EXEC_MATCH_TOL_S:
                continue
            if best_dt is None or d < best_dt:
                best_dt, best_ti = d, ti
        if best_ti is not None:
            linked[ts] = {
                "trade": trades[best_ti],
                "confidence": 1.0,
                "detail": {"match": "exact_tag", "exec_tag": ex["exec_tag"],
                           "dt_seconds": round(best_dt or 0.0, 1)},
            }
            used_trades.add(best_ti)

    # --- Pass 2: heuristic for everything still unlinked ----------------
    candidates: list[tuple[float, str, int, dict]] = []
    for rec in signals:
        if rec.get("deleted"):
            continue
        ts = rec.get("timestamp")
        if not ts or ts in linked:
            continue
        for ti, trade in enumerate(trades):
            if ti in used_trades:
                continue
            scored = _score(rec, trade, config)
            if scored is None:
                continue
            conf, detail = scored
            if conf >= floor:
                candidates.append((conf, ts, ti, detail))

    candidates.sort(key=lambda c: c[0], reverse=True)
    for conf, ts, ti, detail in candidates:
        if ts in linked or ti in used_trades:
            continue
        linked[ts] = {
            "trade": trades[ti],
            "confidence": round(conf, 3),
            "detail": detail,
        }
        used_trades.add(ti)
    return linked


def build_links(*, accounts: list[str] | str | None = None) -> dict[str, dict]:
    """Load real fills + signals from disk and return the link map.

    ``accounts`` flows through db's visibility gate (None -> all visible). When
    auto-exec lands, pass the single selected account here to scope linkage to
    just that account."""
    fills = db.fetch_fills_for_derivation(account=accounts)
    trades = derive_trades(fills)
    sigs = signal_storage.load_all(_tradebot_bridge.SIGNALS_LOG)
    config = instruments.load_config()
    return link_signals_to_trades(list(sigs.values()), trades, config)


def _main() -> None:
    """Dry run against live data: print matches + confidence, no writes."""
    logging.basicConfig(level=logging.WARNING)
    fills = db.fetch_fills_for_derivation()
    trades = derive_trades(fills)
    sigs = signal_storage.load_all(_tradebot_bridge.SIGNALS_LOG)
    config = instruments.load_config()
    active = [r for r in sigs.values()
              if not r.get("deleted")
              and (r.get("proposal") or {}).get("direction") in ("long", "short")]

    links = link_signals_to_trades(active, trades, config)

    print(f"signals (active, non-flat): {len(active)}")
    print(f"round-trip trades:          {len(trades)}")
    print(f"linked:                     {len(links)}")
    print("-" * 88)
    for rec in sorted(active, key=lambda r: r.get("timestamp") or ""):
        ts = rec.get("timestamp")
        p = rec.get("proposal") or {}
        hit = links.get(ts)
        head = (f"{ts}  {str(p.get('instrument') or '?'):10s} "
                f"{(p.get('direction') or '?'):5s} entry={p.get('entry')}")
        if not hit:
            print(f"[UNLINKED] {head}")
            continue
        t = hit["trade"]
        d = hit["detail"]
        exits = ", ".join(f"{f['qty']}@{f['price']}" for f in t.get("exit_fills", []))
        print(f"[{hit['confidence']:.2f}]    {head}")
        print(f"            -> trade {t['direction']} {t['qty']} {t['contract']} "
              f"entry={t['entry_price']} net=${t['net_pnl']} "
              f"acct={t['account']} atm={t.get('strategies')}")
        print(f"            exits: {exits}  ({d})")
    print("-" * 88)


if __name__ == "__main__":
    _main()
