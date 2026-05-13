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


def _parse_one(xml_path: Path) -> dict[str, Any]:
    """Best-effort parse of an ATM strategy XML. Returns name + a handful of
    summary fields. Unknown structure / parse errors yield name-only."""
    info: dict[str, Any] = {"name": xml_path.stem}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        # NT XMLs nest the strategy under <NinjaTrader>. Treat the root or its
        # first child uniformly.
        node = root if root.tag.lower().endswith("atmstrategy") else next(iter(root), root)

        # NT8 schema: <NinjaTrader>/<AtmStrategy>/<Brackets>/<Bracket>
        # Each bracket carries Quantity, StopLoss, Target. Sum up the qty,
        # capture min stop ticks and max target ticks across brackets.
        brackets = node.findall(".//Brackets/Bracket")
        if brackets:
            total_qty   = 0
            stop_ticks  = []
            target_ticks = []
            for b in brackets:
                qty = b.findtext("Quantity")
                if qty:
                    try: total_qty += int(qty)
                    except ValueError: pass
                sl = b.findtext("StopLoss")
                tp = b.findtext("Target")
                if sl:
                    try: stop_ticks.append(int(sl))
                    except ValueError: pass
                if tp:
                    try: target_ticks.append(int(tp))
                    except ValueError: pass
            info["bracket_count"] = len(brackets)
            info["total_qty"]    = total_qty
            if stop_ticks:    info["stop_ticks_min"]   = min(stop_ticks)
            if target_ticks:  info["target_ticks_max"] = max(target_ticks)

        # Break-even and trailing flags, if present.
        for tag in ("AutoBreakEvenPlusProfit", "AutoTrail", "AutoChase"):
            v = node.findtext(tag)
            if v is not None:
                info[tag] = v
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
