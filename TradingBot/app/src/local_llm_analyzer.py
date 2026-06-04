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
 "reasoning": "<1-2 sentences explaining what you saw, especially whether entry was reached>"}}"""


def analyze(image_path: Path, prompt: str, instrument: str | None = None) -> dict:
    """Returns {proposal, raw_response, duration_s}. Raises on HTTP or parse error.
    Dispatches to the configured provider (ollama / claude / openai)."""
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")

    # Inject the ATM strategy menu the LLM must pick from, scoped to this
    # instrument so it can't pick a crude-oil bracket for an MES trade.
    # Prepended so it's visible BEFORE the schema instructions at the bottom.
    atm_strategies = _filter_atm_for_instrument(_load_atm_strategies(), instrument)
    full_prompt = _format_atm_block(atm_strategies) + "\n\n---\n\n" + prompt

    provider = runtime_config.provider("signal")
    logger.info("Analyze via %s (image=%s, instrument=%s, atm_strategies=%d)",
                provider, image_path.name, instrument, len(atm_strategies))

    if provider == "claude":
        raw, duration_s, model_used = _call_claude(image_b64, full_prompt)
    elif provider == "openai":
        raw, duration_s, model_used = _call_openai(image_b64, full_prompt)
    else:  # ollama
        raw, duration_s, model_used = _call_ollama(image_b64, full_prompt)

    proposal = _parse_json(raw)
    proposal["model"] = model_used
    proposal["provider"] = provider

    # Derive stop/target from the picked ATM strategy. Must happen BEFORE
    # tick rounding so the snap respects the chosen brackets.
    _derive_stop_target(proposal, atm_strategies)

    instruments.apply_tick_rounding(proposal, instruments.load_config())
    proposal["risk_reward"] = _compute_rr(proposal)
    return {"proposal": proposal, "raw_response": raw, "duration_s": duration_s}


def analyze_text(prompt: str, instrument: str | None = None) -> dict:
    """Text-only sibling of analyze(). Used by the headless analyzer when no
    fresh chart screenshot is available.

    Parity with analyze(): injects the same ATM strategy menu so the model
    picks a real template, then derives stop/target from it. A directional
    proposal MUST carry an ATM -- the auto-trader places ATM templates, so a
    proposal without one has nothing to execute. Returns the analyze() shape
    ({proposal, raw_response, duration_s}) plus the model/provider keys the
    headless caller logs.
    """
    atm_strategies = _filter_atm_for_instrument(_load_atm_strategies(), instrument)
    full_prompt = _format_atm_block(atm_strategies) + "\n\n---\n\n" + prompt

    provider = runtime_config.provider("signal")
    logger.info("Analyze (text-only) via %s (instrument=%s, atm_strategies=%d)",
                provider, instrument, len(atm_strategies))
    if provider == "claude":
        raw, duration_s, model_used = _call_claude(None, full_prompt)
    elif provider == "openai":
        raw, duration_s, model_used = _call_openai(None, full_prompt)
    else:
        raw, duration_s, model_used = _call_ollama(None, full_prompt)

    proposal = _parse_json(raw)
    proposal["model"] = model_used
    proposal["provider"] = provider
    # The prompt has the LLM echo "instrument", but trust the caller's value
    # when given so the tick-size lookup in _derive_stop_target can't miss.
    if instrument:
        proposal["instrument"] = instrument

    # Derive stop/target from the picked ATM (before tick rounding, like analyze()).
    _derive_stop_target(proposal, atm_strategies)
    instruments.apply_tick_rounding(proposal, instruments.load_config())
    proposal["risk_reward"] = _compute_rr(proposal)

    return {
        "proposal":     proposal,
        "raw_response": raw,
        "duration_s":   duration_s,
        "model":        model_used,
        "provider":     provider,
    }


# ------------------------------------------------------------------ providers

def _call_ollama(image_b64: str | None, prompt: str) -> tuple[str, float, str]:
    """Local Ollama HTTP API. image_b64=None for text-only headless calls.
    Returns (raw_response_text, duration_s, model)."""
    url = runtime_config.ollama_url()
    model_name = runtime_config.model()
    timeout = runtime_config.request_timeout_s()
    num_ctx = runtime_config.num_ctx()
    logger.info("POST %s (model=%s, num_ctx=%d, image=%s)",
                url, model_name, num_ctx, "yes" if image_b64 else "no")
    payload: dict = {
        "model":   model_name,
        "prompt":  prompt,
        "format":  "json",
        "stream":  False,
        "options": {"num_ctx": num_ctx},
    }
    if image_b64:
        payload["images"] = [image_b64]
    resp = requests.post(url, timeout=timeout, json=payload)
    resp.raise_for_status()
    body = resp.json()
    duration_s = body.get("total_duration", 0) / 1e9
    return body["response"], duration_s, model_name


def _call_claude(image_b64: str | None, prompt: str) -> tuple[str, float, str]:
    """Anthropic Messages API. image_b64=None for text-only headless calls."""
    api_key = runtime_config.claude_api_key()
    if not api_key:
        raise RuntimeError("Provider=claude but no claude_api_key in settings")
    model_name  = runtime_config.claude_model()
    max_tokens  = runtime_config.claude_max_tokens()
    timeout     = runtime_config.request_timeout_s()
    logger.info("POST api.anthropic.com (model=%s, max_tokens=%d, image=%s)",
                model_name, max_tokens, "yes" if image_b64 else "no")
    content: list = []
    if image_b64:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": image_b64,
        }})
    content.append({"type": "text", "text": prompt})
    import time as _t
    t0 = _t.monotonic()
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        timeout=timeout,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model_name,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        },
    )
    resp.raise_for_status()
    body = resp.json()
    # content is an array of blocks; concatenate text blocks.
    parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
    raw = "".join(parts)
    return raw, _t.monotonic() - t0, model_name


def _call_openai(image_b64: str | None, prompt: str) -> tuple[str, float, str]:
    """OpenAI Chat Completions. image_b64=None for text-only headless calls."""
    api_key = runtime_config.openai_api_key()
    if not api_key:
        raise RuntimeError("Provider=openai but no openai_api_key in settings")
    model_name = runtime_config.openai_model()
    max_tokens = runtime_config.openai_max_tokens()
    timeout    = runtime_config.request_timeout_s()
    logger.info("POST api.openai.com (model=%s, max_tokens=%d, image=%s)",
                model_name, max_tokens, "yes" if image_b64 else "no")
    content: list = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{image_b64}",
        }})
    import time as _t
    t0 = _t.monotonic()
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        },
    )
    resp.raise_for_status()
    body = resp.json()
    raw = body["choices"][0]["message"]["content"]
    return raw, _t.monotonic() - t0, model_name


def reconcile(image_path: Path, prior_proposal: dict) -> dict:
    """Ask the model whether a prior trade has resolved, given a NEW chart of
    the same instrument. Returns {result, reasoning}.

    Dispatches to the configured provider (ollama / claude / openai)."""
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    prompt = RECONCILE_PROMPT.format(
        instrument=prior_proposal.get("instrument", ""),
        direction=prior_proposal.get("direction", ""),
        entry=prior_proposal.get("entry", ""),
        stop=prior_proposal.get("stop", ""),
        target=prior_proposal.get("target", ""),
    )
    provider = runtime_config.provider()
    logger.info("Reconcile via %s (image=%s)", provider, image_path.name)
    if provider == "claude":
        raw, dt, _ = _call_claude(image_b64, prompt)
    elif provider == "openai":
        raw, dt, _ = _call_openai(image_b64, prompt)
    else:
        raw, dt, _ = _call_ollama(image_b64, prompt)
    logger.info("Reconcile responded in %.1fs", dt)
    return _parse_json(raw)


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


def _parse_bracket(bracket_node: ET.Element) -> dict:
    """Parse a single <Bracket> element into a structured dict.

    Captures the full per-bracket plan -- qty, initial stop, target, auto-BE
    trigger/offset, and the trail-step list. The trail steps drive the
    runner state machine in outcome_resolver."""
    def _int(parent: ET.Element | None, tag: str) -> int:
        if parent is None:
            return 0
        v = parent.findtext(tag)
        try:
            return int(v) if v is not None else 0
        except ValueError:
            return 0

    out: dict = {
        "qty":            _int(bracket_node, "Quantity"),
        "stop_ticks":     _int(bracket_node, "StopLoss"),
        "target_ticks":   _int(bracket_node, "Target"),
        "auto_be_plus":   0,
        "auto_be_trigger": 0,
        "trail_steps":    [],
    }
    ss = bracket_node.find("StopStrategy")
    if ss is not None:
        out["auto_be_plus"]    = _int(ss, "AutoBreakEvenPlus")
        out["auto_be_trigger"] = _int(ss, "AutoBreakEvenProfitTrigger")
        for step in ss.findall("AutoTrailSteps/AutoTrailStep"):
            out["trail_steps"].append({
                "frequency":      _int(step, "Frequency"),
                "profit_trigger": _int(step, "ProfitTrigger"),
                "stop_loss":      _int(step, "StopLoss"),
            })
    return out


def _load_atm_strategies() -> list[dict]:
    """Enumerate NT's ATM strategy templates with full per-bracket detail.

    Reads ~/Documents/NinjaTrader 8/templates/AtmStrategy/*.xml on every call --
    cheap (<10 ms for a typical folder) and ensures user-created strategies
    are picked up without restarting the bot.

    Each strategy carries:
      name, total_qty, brackets[]: {qty, stop_ticks, target_ticks,
        auto_be_plus, auto_be_trigger, trail_steps[]}
    Plus aggregate stop_ticks/target_ticks (tightest/widest) for the
    prompt block and back-compat with old single-bracket consumers.
    """
    if not ATM_TEMPLATES_DIR.is_dir():
        logger.warning("ATM templates dir not found: %s", ATM_TEMPLATES_DIR)
        return []
    out: list[dict] = []
    for xml_path in sorted(ATM_TEMPLATES_DIR.glob("*.xml")):
        info: dict = {
            "name":          xml_path.stem,
            "stop_ticks":    None,
            "target_ticks":  None,
            "total_qty":     0,
            "brackets":      [],
        }
        try:
            root = ET.parse(xml_path).getroot()
            # NT8 XML schema: <NinjaTrader>/<AtmStrategy>/<Brackets>/<Bracket>
            bracket_nodes = root.findall(".//Brackets/Bracket")
            if bracket_nodes:
                brackets = [_parse_bracket(b) for b in bracket_nodes]
                info["brackets"]  = brackets
                info["total_qty"] = sum(b["qty"] for b in brackets if b["qty"] > 0)
                stops = [b["stop_ticks"]   for b in brackets if b["stop_ticks"]   > 0]
                tgts  = [b["target_ticks"] for b in brackets if b["target_ticks"] > 0]
                if stops: info["stop_ticks"]   = min(stops)
                if tgts:  info["target_ticks"] = max(tgts)
        except (ET.ParseError, OSError, ValueError) as e:
            logger.warning("[atm] couldn't parse %s: %s", xml_path.name, e)
        if info["stop_ticks"] and info["target_ticks"]:
            out.append(info)
    return out


def _format_atm_block(strategies: list[dict]) -> str:
    """Build the 'Available ATM Strategies' prompt section the LLM picks from.

    Surfaces total contract count + bracket count for scale-out strategies so
    the model understands sizing implications before picking."""
    if not strategies:
        return ("## Available ATM Strategies\n"
                "(none found in NinjaTrader 8/templates/AtmStrategy/ -- "
                "the bot will fall back to a default 1:2 stop/target.)")
    lines = ["## Available ATM Strategies (pick one by exact name):"]
    for s in strategies:
        rr = s["target_ticks"] / s["stop_ticks"] if s["stop_ticks"] else 0
        qty   = s.get("total_qty") or 1
        bcnt  = len(s.get("brackets") or []) or 1
        size_note = "" if (qty == 1 and bcnt == 1) else f", {qty}c in {bcnt} brackets (scale-out)"
        lines.append(f'- "{s["name"]}": stop_ticks={s["stop_ticks"]}, '
                     f'target_ticks={s["target_ticks"]} (R:R={rr:.1f}{size_note})')
    return "\n".join(lines)


def _atm_root(name: str) -> str:
    """Instrument root an ATM template is scoped to -- the prefix before the
    first underscore (e.g. 'MES_INTRA_1c_16-40' -> 'MES'). Every template in
    NinjaTrader 8/templates/AtmStrategy follows this {ROOT}_... convention."""
    return name.split("_", 1)[0].upper()


def _filter_atm_for_instrument(strategies: list[dict], instrument: str | None) -> list[dict]:
    """Keep only templates whose root matches *instrument* so the LLM can't pick
    a crude-oil bracket for an MES trade. Returns the matched subset (possibly
    empty -- the menu then shows 'none' and the proposal is dismissed rather
    than trading a wrong-instrument template). Falls back to the full list only
    when the instrument is unknown; the _derive_stop_target guard still rejects
    a cross-instrument pick in that case."""
    if not instrument:
        return strategies
    root = (instruments.normalize_symbol(instrument) or "").upper()
    if not root:
        return strategies
    return [s for s in strategies if _atm_root(s["name"]) == root]


def _derive_stop_target(proposal: dict, strategies: list[dict]) -> None:
    """After the LLM picks an atm_strategy, fill in stop and target prices
    from the strategy's tick offsets + the entry + the direction + the
    instrument's tick size. Mutates proposal in place. No-op for flat trades."""
    direction = proposal.get("direction")
    if direction == "flat":
        # Flat = no trade taken, so no ATM template applies. Clear whatever the
        # LLM emitted (it often still names one) so the record never implies a
        # bracket that can't fire and the dashboard shows it blank.
        proposal["atm_strategy"]          = ""
        proposal["atm_strategy_resolved"] = False
        proposal["atm_brackets"]          = []
        proposal["atm_total_qty"]         = 0
        proposal.pop("atm_stop_ticks", None)
        proposal.pop("atm_target_ticks", None)
        proposal.setdefault("stop", proposal.get("entry"))
        proposal.setdefault("target", proposal.get("entry"))
        return

    atm_name = proposal.get("atm_strategy")
    # When the LLM picks an ATM, attach the full per-bracket plan so the
    # outcome resolver can run the trail state machine per leg and the
    # dashboard can show "TP1 + Runner" details. Single-bracket ATMs degrade
    # cleanly to a 1-leg plan; unknown / custom ATMs leave brackets empty
    # so legacy single-outcome math kicks in.
    matched_strat: dict | None = None
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
        # Reject a template scoped to a different instrument (e.g. the LLM
        # picking MCL_SCALP for an MES trade) -- its tick offsets are wrong and
        # the auto-trader would place a crude-oil bracket on equities.
        inst_root = (instruments.normalize_symbol(proposal.get("instrument") or "") or "").upper()
        if strat is not None and inst_root and _atm_root(strat["name"]) != inst_root:
            logger.warning("LLM picked cross-instrument atm_strategy=%r for %s; rejecting",
                           atm_name, inst_root)
            strat = None
        if strat is None:
            # Unknown or wrong-instrument template: we cannot place it and its
            # risk is undefined. Clear the ATM so sanity_check dismisses this
            # directional proposal rather than trading a guessed 1:2 default.
            logger.warning("atm_strategy=%r is not a valid %s template; clearing so "
                           "the proposal is dismissed", atm_name, inst_root or "instrument")
            proposal["atm_strategy"]          = ""
            proposal["atm_strategy_resolved"] = False
            proposal["atm_brackets"]          = []
            proposal["atm_total_qty"]         = 0
            return
        else:
            stop_ticks   = int(strat["stop_ticks"])
            target_ticks = int(strat["target_ticks"])
            proposal["atm_strategy_resolved"] = True
            matched_strat = strat

    proposal["atm_stop_ticks"]   = stop_ticks
    proposal["atm_target_ticks"] = target_ticks

    # Scale-out plumbing: when the picked ATM is one we know, surface the
    # full per-bracket plan so the resolver + UI can act on it. Total qty
    # becomes the proposal's sizing hint (the pipeline lifts it onto the
    # signal record's position_size).
    if matched_strat:
        proposal["atm_brackets"]  = matched_strat.get("brackets") or []
        proposal["atm_total_qty"] = int(matched_strat.get("total_qty") or 1)
    else:
        proposal["atm_brackets"]  = []
        proposal["atm_total_qty"] = 1

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
