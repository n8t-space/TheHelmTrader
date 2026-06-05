"""Instrument tick-size lookup and price snapping.

Reads instruments.json (futures explicit map + forex/stock fallback rules).
Used post-parse to snap LLM-proposed prices to valid tick increments and to
annotate any adjustments so the dashboard can surface model failures.
"""
import json
import logging
import re
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "instruments.json"

# Strip a futures contract suffix from the chart's instrument string.
# Matches either single-letter month codes ("H26", "M26") or 3-letter month
# abbreviations ("JUN26", "DEC25"), with or without a leading space.
_CONTRACT_SUFFIX = re.compile(
    r"\s*(?:[FGHJKMNQUVXZ]\d{2}|"
    r"JAN\d{2}|FEB\d{2}|MAR\d{2}|APR\d{2}|MAY\d{2}|JUN\d{2}|"
    r"JUL\d{2}|AUG\d{2}|SEP\d{2}|OCT\d{2}|NOV\d{2}|DEC\d{2})\s*$"
)

_FOREX_PATTERN = re.compile(r"^[A-Z]{6}$")
_STOCK_PATTERN = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        logger.warning("instruments.json not found at %s; using empty config", path)
        return {"instruments": {}, "rules": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_symbol(symbol: str) -> str:
    """Normalize a chart's instrument string to a lookup root.

    Examples:
        "MES JUN26"  -> "MES"
        "ESH26"      -> "ES"
        "AAPL"       -> "AAPL"
        "EUR/USD"    -> "EURUSD"
    """
    if not symbol:
        return ""
    s = symbol.strip().upper().replace("/", "").replace("-", "").replace("_", "")
    s = _CONTRACT_SUFFIX.sub("", s).strip()
    return s


def lookup_tick_size(symbol: str, config: dict) -> tuple[float | None, str]:
    """Return (tick_size, source). tick_size is None if symbol is unknown."""
    root = normalize_symbol(symbol)
    if not root:
        return None, "empty"

    instruments = config.get("instruments", {})
    rules = config.get("rules", {})

    if root in instruments:
        return float(instruments[root]), "explicit"

    if _FOREX_PATTERN.match(root):
        if "JPY" in root and "forex_jpy_tick" in rules:
            return float(rules["forex_jpy_tick"]), "forex_jpy"
        if "forex_other_tick" in rules:
            return float(rules["forex_other_tick"]), "forex_other"

    if _STOCK_PATTERN.match(root) and "stock_default_tick" in rules:
        return float(rules["stock_default_tick"]), "stock_default"

    return None, "unknown"


def lookup_point_value(symbol: str, config: dict) -> float | None:
    """Return the dollar value of one full price point per contract/share.

    None if unknown. Forex default assumes a standard lot (100,000 base);
    user adjusts position_size for mini/micro lots.
    """
    root = normalize_symbol(symbol)
    if not root:
        return None

    point_values = config.get("point_values", {})
    rules = config.get("rules", {})

    if root in point_values:
        return float(point_values[root])

    if _FOREX_PATTERN.match(root) and "forex_default_point_value" in rules:
        return float(rules["forex_default_point_value"])

    if _STOCK_PATTERN.match(root) and "stock_default_point_value" in rules:
        return float(rules["stock_default_point_value"])

    return None


def lookup_commission_rt(symbol: str, config: dict) -> float:
    """Round-trip commission+fee per contract for an instrument (broker-derived).
    Falls back to the rules default, then 0. Used to net paper P&L for fees so
    a signal's realized P&L is comparable to Trade Performance (which uses the
    real per-fill commission)."""
    root = normalize_symbol(symbol)
    table = config.get("commission_per_rt", {})
    if root in table:
        return float(table[root])
    return float(config.get("rules", {}).get("default_commission_per_rt", 0.0))


def compute_trade_metrics(rec: dict, config: dict) -> dict:
    """Compute dollar amounts and (if applicable) realized P/L for a signal record.

    Display unit: futures (anything in the explicit instruments map) → ticks.
    Stocks/forex → points. The metrics dict carries both; templates branch on `display_mode`.

    P/L logic: if outcome.closing_price is set, realized = direction × (close − entry) × pv × size.
    Otherwise derive from outcome.result (target → +reward, stop → −risk, breakeven → 0).
    """
    proposal = rec.get("proposal") or {}
    direction = proposal.get("direction")
    instrument = proposal.get("instrument", "")
    point_value = lookup_point_value(instrument, config)
    tick_size, tick_source = lookup_tick_size(instrument, config)
    is_futures = tick_source == "explicit"
    # Contract count for sizing + P&L. Priority:
    #   1. the actual LEGS that traded (ground truth for a resolved scale-out),
    #   2. the ATM template's total qty (it fixes order size -- a multi-bracket
    #      template places >1; a stale position_size=1 must not override it),
    #   3. an explicit position_size override (manual / non-ATM signals),
    #   4. 1.
    # This keeps Qty / total_risk / total_reward consistent with realized P&L,
    # which sums each leg's qty below.
    def _num(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    _legs = rec.get("legs")
    leg_qty = (sum(_num(l.get("qty")) for l in _legs if isinstance(l, dict))
               if isinstance(_legs, list) else 0.0)
    atm_qty     = _num(proposal.get("atm_total_qty"))
    explicit_sz = _num(rec.get("position_size"))
    position_size = leg_qty or atm_qty or explicit_sz or 1.0
    outcome = rec.get("outcome") or {}
    outcome_result = outcome.get("result")
    closing_price = outcome.get("closing_price")

    base = {
        "point_value": point_value,
        "tick_size": tick_size,
        "tick_value": (tick_size * point_value) if (tick_size and point_value) else None,
        "display_mode": "ticks" if is_futures else "points",
        "position_size": position_size,
        "risk_points": 0.0,
        "reward_points": 0.0,
        "risk_ticks": 0.0,
        "reward_ticks": 0.0,
        "risk_per_contract": 0.0,
        "reward_per_contract": 0.0,
        "total_risk": 0.0,
        "total_reward": 0.0,
        "realized_pnl": None,
        "realized_pnl_source": None,
        "commission": 0.0,
        "leg_breakdown": None,
    }

    if direction == "flat" or point_value is None:
        return base

    try:
        entry = float(proposal.get("entry") or 0)
        stop = float(proposal.get("stop") or 0)
        target = float(proposal.get("target") or 0)
    except (TypeError, ValueError):
        return base

    risk_points = abs(entry - stop)
    reward_points = abs(target - entry)
    risk_ticks = (risk_points / tick_size) if tick_size else 0.0
    reward_ticks = (reward_points / tick_size) if tick_size else 0.0

    risk_per_contract = risk_points * point_value
    reward_per_contract = reward_points * point_value
    total_risk = risk_per_contract * position_size
    total_reward = reward_per_contract * position_size

    realized_pnl = None
    realized_pnl_source = None

    # Auditor override: when the integrity auditor has matched this signal to its
    # real NT8 round-trip, it stamps the actual broker net P&L here. NT fills are
    # ground truth, so this outranks the paper resolver's leg walk -- it's what
    # flips a falsely-"won" paper trade to the loss the account actually took.
    audit = rec.get("audit") or {}
    if audit.get("source") == "fills" and audit.get("realized_pnl") is not None:
        try:
            realized_pnl = float(audit["realized_pnl"])
            realized_pnl_source = "fills"
            if audit.get("real_qty"):
                position_size = _num(audit["real_qty"]) or position_size
        except (TypeError, ValueError):
            realized_pnl = None
            realized_pnl_source = None

    # Per-leg fills (multi-bracket scale-out) take precedence over everything.
    # Sum the realized P&L across legs; record the per-leg breakdown for the UI.
    legs = rec.get("legs")
    leg_breakdown: list[dict] | None = None
    if isinstance(legs, list) and legs and direction in ("long", "short"):
        sign = 1 if direction == "long" else -1
        pnl_sum = 0.0
        leg_breakdown = []
        any_leg = False
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            try:
                qty = float(leg.get("qty") or 0)
                px  = float(leg.get("exit_price"))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            # A leg the auditor wrote from real fills carries its own exact dollar
            # P&L; trust it over a recompute off the (planned) entry price.
            stored_pnl = leg.get("pnl")
            if isinstance(stored_pnl, (int, float)):
                leg_pnl = float(stored_pnl)
            else:
                leg_pnl = sign * (px - entry) * point_value * qty
            pnl_sum += leg_pnl
            any_leg = True
            leg_breakdown.append({
                "bracket_idx":   leg.get("bracket_idx"),
                "qty":           qty,
                "result":        leg.get("result"),
                "exit_price":    px,
                "exit_ts":       leg.get("exit_ts"),
                "method":        leg.get("method"),
                "pnl":           leg_pnl,
            })
        # Don't clobber an auditor (real-fills) override with the paper leg walk;
        # the breakdown above is still useful to display either way.
        if any_leg and realized_pnl is None:
            realized_pnl        = pnl_sum
            realized_pnl_source = "legs"

    # Closing price overrides the single-outcome path (still useful for trades
    # where the user just wants to type the average fill rather than enter legs).
    if realized_pnl is None and closing_price not in (None, "") \
       and direction in ("long", "short") and position_size > 0:
        try:
            close = float(closing_price)
            sign = 1 if direction == "long" else -1
            realized_pnl = sign * (close - entry) * point_value * position_size
            realized_pnl_source = "closing_price"
        except (TypeError, ValueError):
            pass

    if realized_pnl is None and position_size > 0:
        if outcome_result == "target":
            realized_pnl = total_reward
            realized_pnl_source = "target"
        elif outcome_result == "stop":
            realized_pnl = -total_risk
            realized_pnl_source = "stop"
        elif outcome_result == "breakeven":
            realized_pnl = 0.0
            realized_pnl_source = "breakeven"

    # Account for fees, like Trade Performance. The 'fills' path is already net
    # (the auditor used the real per-fill commission); expose that fee for the
    # UI. The paper paths (legs/closing_price/target/stop) are GROSS, so deduct
    # the broker-derived round-trip estimate so the displayed P&L is net too.
    commission = 0.0
    if realized_pnl is not None and position_size > 0:
        if realized_pnl_source == "fills":
            gross = (rec.get("audit") or {}).get("real_gross_pnl")
            if gross is not None:
                commission = round(float(gross) - float(realized_pnl), 2)
        else:
            commission = round(lookup_commission_rt(instrument, config) * position_size, 2)
            realized_pnl = round(float(realized_pnl) - commission, 2)

    return {
        "point_value": point_value,
        "tick_size": tick_size,
        "tick_value": (tick_size * point_value) if tick_size else None,
        "display_mode": "ticks" if is_futures else "points",
        "position_size": position_size,
        "risk_points": risk_points,
        "reward_points": reward_points,
        "risk_ticks": risk_ticks,
        "reward_ticks": reward_ticks,
        "risk_per_contract": risk_per_contract,
        "reward_per_contract": reward_per_contract,
        "total_risk": total_risk,
        "total_reward": total_reward,
        "realized_pnl": realized_pnl,
        "realized_pnl_source": realized_pnl_source,
        "commission": commission,
        "leg_breakdown": leg_breakdown,
    }


def snap_to_tick(price: float, tick_size: float) -> float:
    """Snap price to the nearest tick using banker's rounding (Decimal-precise)."""
    if not tick_size or tick_size <= 0:
        return price
    p = Decimal(str(price))
    t = Decimal(str(tick_size))
    snapped = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * t
    return float(snapped)


def apply_tick_rounding(proposal: dict, config: dict) -> dict:
    """Snap entry/stop/target to the instrument's tick size and annotate the proposal.

    Mutates and returns `proposal`. Adds these fields:
        tick_size_applied: float | None  -- what tick we used (None if unknown)
        tick_source:       str           -- explicit / forex_jpy / forex_other / stock_default / unknown / empty
        tick_adjustments:  list[dict]    -- one entry per field that was snapped: {field, from, to}
    """
    instrument = proposal.get("instrument", "")
    tick_size, source = lookup_tick_size(instrument, config)

    proposal["tick_size_applied"] = tick_size
    proposal["tick_source"] = source

    if tick_size is None:
        logger.warning(
            "Unknown instrument %r — no tick rounding applied. "
            "Add it to instruments.json if you trade this regularly.",
            instrument,
        )
        proposal["tick_adjustments"] = []
        return proposal

    if proposal.get("direction") == "flat":
        # Sentinel zeros for flat; nothing to snap.
        proposal["tick_adjustments"] = []
        return proposal

    adjustments = []
    for field in ("entry", "stop", "target"):
        original = proposal.get(field)
        if original is None or not isinstance(original, (int, float)):
            continue
        snapped = snap_to_tick(float(original), tick_size)
        if snapped != float(original):
            adjustments.append({"field": field, "from": float(original), "to": snapped})
            proposal[field] = snapped

    proposal["tick_adjustments"] = adjustments
    if adjustments:
        logger.info(
            "Tick-snapped %d field(s) for %s (tick=%s, source=%s)",
            len(adjustments), instrument, tick_size, source,
        )
    return proposal
