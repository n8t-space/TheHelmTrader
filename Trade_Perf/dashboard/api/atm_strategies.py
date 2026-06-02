"""Enumerate NinjaTrader ATM strategy templates.

NT8 stores ATM strategy templates as XML files at:
    ~/Documents/NinjaTrader 8/templates/AtmStrategy/*.xml

Each filename (minus the .xml extension) is the strategy name as it appears
in NT's ATM dropdown. Some fields we parse from the XML so the dashboard can
show them at a glance (profit-target ticks, stop-loss ticks, BE behavior).

Read on every request rather than caching at process start -- the user
sometimes creates a new ATM strategy in NT mid-session and expects the
dashboard to pick it up without a restart. The dir read is cheap (<10 ms
for a typical 5-20 strategy folder).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/atm-strategies", tags=["atm-strategies"])

ATM_TEMPLATES_DIR = Path.home() / "Documents" / "NinjaTrader 8" / "templates" / "AtmStrategy"


def _int_or_none(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_bracket(b: ET.Element) -> dict[str, Any]:
    """Parse one <Bracket> -- quantity, stop, target, and the embedded
    <StopStrategy> (template name + AutoBreakEven + AutoTrailSteps).

    NT8 schema (the actual one, not the one the old parser guessed at):
        <Bracket>
          <Quantity/> <StopLoss/> <Target/>
          <StopStrategy>
            <AutoBreakEvenPlus/>              tick offset moved to entry
            <AutoBreakEvenProfitTrigger/>     profit ticks at which BE arms
            <AutoTrailSteps>
              <AutoTrailStep>
                <Frequency/>                  step size (ticks)
                <ProfitTrigger/>              profit ticks at which step fires
                <StopLoss/>                   new stop distance after firing
              </AutoTrailStep>
              ...
            </AutoTrailSteps>
            <Template/>                       sibling stop-strategy template
          </StopStrategy>
        </Bracket>
    """
    out: dict[str, Any] = {
        "quantity":                 _int_or_none(b.findtext("Quantity")),
        "stop_loss_ticks":          _int_or_none(b.findtext("StopLoss")),
        "target_ticks":             _int_or_none(b.findtext("Target")),
        "stop_strategy_template":   None,
        "break_even_offset_ticks":  None,
        "break_even_trigger_ticks": None,
        "trail_steps":              [],
    }
    ss = b.find("StopStrategy")
    if ss is None:
        return out
    tpl = (ss.findtext("Template") or "").strip()
    if tpl:
        out["stop_strategy_template"] = tpl
    be_off = _int_or_none(ss.findtext("AutoBreakEvenPlus"))
    be_trg = _int_or_none(ss.findtext("AutoBreakEvenProfitTrigger"))
    if be_off and be_off != 0:
        out["break_even_offset_ticks"] = be_off
    if be_trg and be_trg != 0:
        out["break_even_trigger_ticks"] = be_trg
    steps: list[dict[str, int | None]] = []
    for st in ss.findall("AutoTrailSteps/AutoTrailStep"):
        steps.append({
            "profit_trigger_ticks": _int_or_none(st.findtext("ProfitTrigger")),
            "frequency_ticks":      _int_or_none(st.findtext("Frequency")),
            "stop_loss_ticks":      _int_or_none(st.findtext("StopLoss")),
        })
    out["trail_steps"] = steps
    return out


def _parse_one(xml_path: Path) -> dict[str, Any]:
    """Best-effort parse of an ATM strategy XML. Returns name + summary
    fields + per-bracket detail (incl. embedded StopStrategy). Unknown
    structure / parse errors yield name-only."""
    info: dict[str, Any] = {"name": xml_path.stem}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        # NT XMLs nest the strategy under <NinjaTrader>. Treat the root or its
        # first child uniformly.
        node = root if root.tag.lower().endswith("atmstrategy") else next(iter(root), root)

        brackets = node.findall(".//Brackets/Bracket")
        parsed_brackets = [_parse_bracket(b) for b in brackets]
        info["brackets"]      = parsed_brackets
        info["bracket_count"] = len(parsed_brackets)
        info["total_qty"]     = sum((b["quantity"] or 0) for b in parsed_brackets)

        stops   = [b["stop_loss_ticks"] for b in parsed_brackets if b["stop_loss_ticks"] is not None]
        targets = [b["target_ticks"]    for b in parsed_brackets if b["target_ticks"]    is not None]
        if stops:   info["stop_ticks_min"]   = min(stops)
        if targets: info["target_ticks_max"] = max(targets)

        # 'has_stop_strategy' = any bracket references a stop-strategy template
        # OR has a non-zero BE offset OR has any trail steps. Surfaces the
        # bracket-level reality so the UI doesn't have to re-derive it.
        info["has_stop_strategy"] = any(
            (b["stop_strategy_template"] is not None)
            or (b["break_even_offset_ticks"] not in (None, 0))
            or bool(b["trail_steps"])
            for b in parsed_brackets
        )
    except (ET.ParseError, OSError) as e:
        logger.warning("[atm-strategies] could not parse %s: %s", xml_path.name, e)
    return info


@router.get("")
def list_strategies() -> dict[str, Any]:
    """Return all ATM strategies the local NT install knows about."""
    if not ATM_TEMPLATES_DIR.is_dir():
        return {
            "templates_dir": str(ATM_TEMPLATES_DIR),
            "exists": False,
            "strategies": [],
            "warning": "NT8 templates folder not found -- is NinjaTrader installed?",
        }
    xmls = sorted(ATM_TEMPLATES_DIR.glob("*.xml"))
    return {
        "templates_dir": str(ATM_TEMPLATES_DIR),
        "exists": True,
        "count": len(xmls),
        "strategies": [_parse_one(p) for p in xmls],
    }


@router.get("/names")
def list_names() -> dict[str, Any]:
    """Cheap path -- just the names, for dropdowns. No XML parsing."""
    if not ATM_TEMPLATES_DIR.is_dir():
        return {"names": [], "exists": False}
    names = sorted(p.stem for p in ATM_TEMPLATES_DIR.glob("*.xml"))
    return {"names": names, "exists": True, "count": len(names)}
