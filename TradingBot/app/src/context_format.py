"""Shared NinjaScript-context -> prompt formatter.

Both the manual hotkey path (pipeline.py) and the auto/headless path
(headless_analyzer.py) prepend the same authoritative-context block so the
two flows reason over identical, verified numbers -- including Smart-Money
market structure (BOS/CHoCH). Keeping one formatter here is what prevents
the two paths from drifting (the whole point of the HelmFeed/HelmAnalyzer
merge).

Input shape is the rich NS context dict (see HelmFeed/HelmAnalyzer
BuildContextJson): {instrument, current{bid,ask,last}, timeframes{...},
daily_levels{...}, market_structure[ {retrace_pct, trend, structure,
last_structure_event, break_price, last_confirmed_high{...},
last_confirmed_low{...}}, ... ]}.
"""
from __future__ import annotations

from typing import Any

_TRUTH_FOOTER = (
    "This context is the VERIFIED source of truth -- treat every price and "
    "indicator value above as authoritative; do not re-read or recompute them "
    "from the chart axis. Use the chart screenshot ONLY for visual structure "
    "(trend, pullbacks, support/resistance, candle patterns). Do not cite "
    "indicator values or levels that are not provided above."
)


def format_ns_context(ctx: dict[str, Any]) -> str:
    """Render the rich NS context dict into the prompt's authoritative block."""
    lines = [
        "## Authoritative Market Context (from NinjaTrader)",
        f"Instrument: {ctx.get('instrument', 'unknown')}",
    ]

    current = ctx.get("current") or {}
    if current:
        lines.append(
            f"Current: bid={current.get('bid')}, ask={current.get('ask')}, "
            f"last={current.get('last')}"
        )

    tfs = ctx.get("timeframes") or {}
    if tfs:
        lines.append("")
        lines.append("Timeframes:")
        for tf_name, tf_data in tfs.items():
            if not isinstance(tf_data, dict):
                continue
            parts = ", ".join(f"{k}={v}" for k, v in tf_data.items() if v is not None)
            lines.append(f"  {tf_name}: {parts}")

    daily = ctx.get("daily_levels") or {}
    if daily:
        lines.append("")
        lines.append("Daily levels:")
        if "pivot_p" in daily:
            lines.append(
                f"  Floor pivots: P={daily.get('pivot_p')}, "
                f"R1={daily.get('pivot_r1')}, R2={daily.get('pivot_r2')}, "
                f"R3={daily.get('pivot_r3')}, S1={daily.get('pivot_s1')}, "
                f"S2={daily.get('pivot_s2')}, S3={daily.get('pivot_s3')}"
            )
        if "today_high" in daily:
            lines.append(f"  Today: high={daily.get('today_high')}, low={daily.get('today_low')}")
        if "yesterday_high" in daily:
            lines.append(
                f"  Yesterday: high={daily.get('yesterday_high')}, "
                f"low={daily.get('yesterday_low')}, close={daily.get('yesterday_close')}"
            )

    for line in _format_structure(ctx.get("market_structure") or []):
        lines.append(line)

    lines.append("")
    lines.append(_TRUTH_FOOTER)
    return "\n".join(lines)


def _format_structure(structure: list) -> list[str]:
    """Market-structure lens block (empty list if no structure present)."""
    if not structure:
        return []
    out = ["", "Market structure (Smart-Money, one read per retrace sensitivity):"]
    for lens in structure:
        if not isinstance(lens, dict):
            continue
        seg = (f"  retrace {lens.get('retrace_pct')}%: trend={lens.get('trend')}, "
               f"structure={lens.get('structure')}, "
               f"last_event={lens.get('last_structure_event')}")
        bp = lens.get("break_price")
        if bp is not None:
            seg += f" @ {bp}"
        ch = lens.get("last_confirmed_high") or {}
        cl = lens.get("last_confirmed_low") or {}
        hi, lo = ch.get("price"), cl.get("price")
        if hi is not None or lo is not None:
            seg += (f"; last confirmed high={hi} ({ch.get('label')}), "
                    f"low={lo} ({cl.get('label')})")
        out.append(seg)
    return out
