"""Send a chart screenshot to the local vision LLM and parse the structured proposal."""
import base64
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from . import instruments, runtime_config

ATM_TEMPLATES_DIR = Path.home() / "Documents" / "NinjaTrader 8" / "templates" / "AtmStrategy"

logger = logging.getLogger(__name__)

# These constants are kept for back-compat with any external importers (tests,
# diagnostics) -- the live values come from runtime_config so the Settings page
# can change them without a restart. Module-load values are a one-shot snapshot.
OLLAMA_URL = runtime_config.ollama_url()
MODEL = runtime_config.model()
TIMEOUT = runtime_config.request_timeout_s()

CONFIDENCE_FLOOR = runtime_config.confidence_floor()
MAX_ATTEMPTS = runtime_config.max_attempts()

RECONCILE_PROMPT = """You previously analyzed a {instrument} chart and proposed:
- Direction: {direction}
- Entry:  {entry}
- Stop:   {stop}
- Target: {target}

You are now examining a NEW chart of the SAME instrument. Trace what happened
in this order:

1. Did price reach the ENTRY level ({entry}) at any point in the visible
   history since the prior chart? If NOT, the trade was never opened — return
   result "no_fill".
2. If entry WAS reached, then determine which level was touched FIRST after
   that point — TARGET ({target}) or STOP ({stop}) — based on the candle
   sequence. Return "target" or "stop" accordingly.
3. If entry was reached but neither target nor stop has been touched yet,
   return "neither" (still open).
4. If you cannot tell from the visible chart, return "uncertain".

Reply with ONLY this JSON:
{{"result": "no_fill" | "target" | "stop" | "neither" | "uncertain",
 "confidence": <0.0-1.0>,
 "reasoning": "<1-2 sentences explaining what you saw, especially whether entry was reached>"}}"""


def analyze(image_path: Path, prompt: str) -> dict:
    """Returns {proposal, raw_response, duration_s}. Raises on HTTP or parse error."""
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    url, model_name, timeout = runtime_config.ollama_url(), runtime_config.model(), runtime_config.request_timeout_s()

    # Inject the ATM strategy menu the LLM must pick from. Prepended to the
    # prompt so it's visible BEFORE the schema instructions at the bottom.
    atm_strategies = _load_atm_strategies()
    full_prompt = _format_atm_block(atm_strategies) + "\n\n---\n\n" + prompt

    logger.info("POST %s (model=%s, image=%s, atm_strategies=%d)",
                url, model_name, image_path.name, len(atm_strategies))
    resp = requests.post(url, timeout=timeout, json={
        "model": model_name,
        "prompt": full_prompt,
        "images": [image_b64],
        "format": "json",
        "stream": False,
    })
    resp.raise_for_status()
    body = resp.json()
    duration_s = body.get("total_duration", 0) / 1e9
    logger.info("Model responded in %.1fs", duration_s)

    raw = body["response"]
    proposal = _parse_json(raw)

    # Derive stop/target from the picked ATM strategy. Must happen BEFORE
    # tick rounding so the snap respects the chosen brackets.
    _derive_stop_target(proposal, atm_strategies)

    instruments.apply_tick_rounding(proposal, instruments.load_config())
    proposal["risk_reward"] = _compute_rr(proposal)
    return {"proposal": proposal, "raw_response": raw, "duration_s": duration_s}


def analyze_with_floor(
    image_path: Path,
    prompt: str,
    floor: float | None = None,
    max_attempts: int | None = None,
) -> dict:
    """Run analyze(); if confidence < floor, retry up to max_attempts. Keep best.

    Annotates the returned proposal with:
        attempts: int             -- how many times the model was called
        reassessed: bool          -- whether at least one retry happened
        attempt_confidences: list -- confidence from each attempt
        confidence_floor: float   -- the threshold in effect
    """
    if floor is None:
        floor = runtime_config.confidence_floor()
    if max_attempts is None:
        max_attempts = runtime_config.max_attempts()
    best = None
    confidences: list[float] = []
    for attempt in range(1, max_attempts + 1):
        result = analyze(image_path, prompt)
        confidence = float(result["proposal"].get("confidence") or 0)
        confidences.append(confidence)
        if best is None or confidence > float(best["proposal"].get("confidence") or 0):
            best = result
        if confidence >= floor:
            break
        if attempt < max_attempts:
            logger.warning(
                "Confidence %.2f < floor %.2f on attempt %d/%d — reassessing",
                confidence, floor, attempt, max_attempts,
            )

    proposal = best["proposal"]
    proposal["attempts"] = len(confidences)
    proposal["reassessed"] = len(confidences) > 1
    proposal["attempt_confidences"] = confidences
    proposal["confidence_floor"] = floor
    return best


def reconcile(image_path: Path, prior_proposal: dict) -> dict:
    """Ask the model whether a prior trade has resolved, given a NEW chart of the same instrument.

    Returns {result, confidence, reasoning}. Raises on HTTP or parse error.
    """
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    prompt = RECONCILE_PROMPT.format(
        instrument=prior_proposal.get("instrument", ""),
        direction=prior_proposal.get("direction", ""),
        entry=prior_proposal.get("entry", ""),
        stop=prior_proposal.get("stop", ""),
        target=prior_proposal.get("target", ""),
    )
    url, model_name, timeout = runtime_config.ollama_url(), runtime_config.model(), runtime_config.request_timeout_s()
    logger.info("Reconciliation POST (model=%s, image=%s)", model_name, image_path.name)
    resp = requests.post(url, timeout=timeout, json={
        "model": model_name,
        "prompt": prompt,
        "images": [image_b64],
        "format": "json",
        "stream": False,
    })
    resp.raise_for_status()
    body = resp.json()
    duration_s = body.get("total_duration", 0) / 1e9
    logger.info("Reconcile responded in %.1fs", duration_s)
    return _parse_json(body["response"])


def _parse_json(raw: str) -> dict:
    """Defensive parse: format='json' should yield clean output, but tolerate fences."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Direct JSON parse failed; trying fence-extraction")
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _load_atm_strategies() -> list[dict]:
    """Enumerate NT's ATM strategy templates with stop/target tick counts.

    Reads ~/Documents/NinjaTrader 8/templates/AtmStrategy/*.xml on every call --
    cheap (<10 ms for a typical folder) and ensures user-created strategies
    are picked up without restarting the bot.
    """
    if not ATM_TEMPLATES_DIR.is_dir():
        logger.warning("ATM templates dir not found: %s", ATM_TEMPLATES_DIR)
        return []
    out: list[dict] = []
    for xml_path in sorted(ATM_TEMPLATES_DIR.glob("*.xml")):
        info = {"name": xml_path.stem, "stop_ticks": None, "target_ticks": None}
        try:
            root = ET.parse(xml_path).getroot()
            # NT8 XML schema: <NinjaTrader>/<AtmStrategy>/<Brackets>/<Bracket>
            brackets = root.findall(".//Brackets/Bracket")
            if brackets:
                stops = [int(b.findtext("StopLoss") or 0) for b in brackets]
                tgts  = [int(b.findtext("Target")   or 0) for b in brackets]
                # Use the tightest stop and widest target across brackets --
                # matches what the user-visible R:R label typically shows.
                info["stop_ticks"]   = min(s for s in stops if s > 0) if any(stops) else None
                info["target_ticks"] = max(t for t in tgts  if t > 0) if any(tgts)  else None
        except (ET.ParseError, OSError, ValueError) as e:
            logger.warning("[atm] couldn't parse %s: %s", xml_path.name, e)
        if info["stop_ticks"] and info["target_ticks"]:
            out.append(info)
    return out


def _format_atm_block(strategies: list[dict]) -> str:
    """Build the 'Available ATM Strategies' prompt section the LLM picks from."""
    if not strategies:
        return ("## Available ATM Strategies\n"
                "(none found in NinjaTrader 8/templates/AtmStrategy/ -- "
                "the bot will fall back to a default 1:2 stop/target.)")
    lines = ["## Available ATM Strategies (pick one by exact name):"]
    for s in strategies:
        rr = s["target_ticks"] / s["stop_ticks"] if s["stop_ticks"] else 0
        lines.append(f'- "{s["name"]}": stop_ticks={s["stop_ticks"]}, '
                     f'target_ticks={s["target_ticks"]} (R:R={rr:.1f})')
    return "\n".join(lines)


def _derive_stop_target(proposal: dict, strategies: list[dict]) -> None:
    """After the LLM picks an atm_strategy, fill in stop and target prices
    from the strategy's tick offsets + the entry + the direction + the
    instrument's tick size. Mutates proposal in place. No-op for flat trades."""
    direction = proposal.get("direction")
    if direction == "flat":
        proposal.setdefault("stop", proposal.get("entry"))
        proposal.setdefault("target", proposal.get("entry"))
        return

    atm_name = proposal.get("atm_strategy")
    if not atm_name:
        logger.warning("LLM did not emit an atm_strategy field; falling back to "
                       "a 1:2 default (10 ticks stop / 20 ticks target)")
        atm_name = None
        stop_ticks, target_ticks = 10, 20
        proposal["atm_strategy_resolved"] = False
    elif atm_name == "custom":
        # LLM is suggesting a custom strategy. Pull the tick counts it
        # provided. Mark resolved=False so the UI can highlight that the
        # user would need to create this strategy in NT to trade as proposed.
        try:
            stop_ticks   = int(proposal.get("custom_stop_ticks", 10))
            target_ticks = int(proposal.get("custom_target_ticks", 20))
        except (TypeError, ValueError):
            logger.warning("LLM said atm_strategy=custom but custom ticks were "
                           "invalid; falling back to 1:2 default")
            stop_ticks, target_ticks = 10, 20
        proposal["atm_strategy_resolved"] = False  # user must create it in NT
        logger.info("LLM proposed CUSTOM ATM strategy: stop=%d target=%d",
                    stop_ticks, target_ticks)
    else:
        strat = next((s for s in strategies if s["name"] == atm_name), None)
        if strat is None:
            logger.warning("LLM picked unknown atm_strategy=%r (not in %s); "
                           "falling back to a 1:2 default",
                           atm_name, [s["name"] for s in strategies])
            stop_ticks, target_ticks = 10, 20
            proposal["atm_strategy_resolved"] = False
        else:
            stop_ticks   = int(strat["stop_ticks"])
            target_ticks = int(strat["target_ticks"])
            proposal["atm_strategy_resolved"] = True

    proposal["atm_stop_ticks"]   = stop_ticks
    proposal["atm_target_ticks"] = target_ticks

    instrument = proposal.get("instrument", "")
    tick_size, _ = instruments.lookup_tick_size(instrument, instruments.load_config())
    if not tick_size or tick_size <= 0:
        logger.warning("No tick_size for %r; leaving stop/target unset", instrument)
        return

    try:
        entry = float(proposal["entry"])
    except (KeyError, TypeError, ValueError):
        logger.warning("LLM did not emit a numeric entry; can't derive stop/target")
        return

    sign = 1.0 if direction == "long" else -1.0
    proposal["stop"]   = entry - sign * stop_ticks   * tick_size
    proposal["target"] = entry + sign * target_ticks * tick_size


def _compute_rr(proposal: dict) -> float:
    """Compute risk:reward in code; ignore whatever the model said."""
    if proposal.get("direction") == "flat":
        return 0.0
    try:
        entry = float(proposal["entry"])
        stop = float(proposal["stop"])
        target = float(proposal["target"])
        risk = abs(entry - stop)
        if risk == 0:
            return 0.0
        return round(abs(target - entry) / risk, 2)
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Could not compute risk_reward: %s", e)
        return 0.0
