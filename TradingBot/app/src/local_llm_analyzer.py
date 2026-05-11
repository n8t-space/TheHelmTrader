"""Send a chart screenshot to the local vision LLM and parse the structured proposal."""
import base64
import json
import logging
import re
from pathlib import Path

import requests

from . import instruments, runtime_config

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

    logger.info("POST %s (model=%s, image=%s)", url, model_name, image_path.name)
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
    logger.info("Model responded in %.1fs", duration_s)

    raw = body["response"]
    proposal = _parse_json(raw)
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
