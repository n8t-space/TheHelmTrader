"""Economic-calendar widget backing the Home page News card.

Two sources:

  ForexFactory  -- public XML feed at
                   https://nfs.faireconomy.media/ff_calendar_thisweek.xml.
                   No AI required. Always tried when news.forexfactory_enabled.

  Econoday      -- https://us.econoday.com/byweek scraped HTML, summarised by
                   the configured AI provider. Requires news.econoday_enabled
                   AND a reachable AI backend. Falls back silently when AI is
                   unreachable so the FF events still render.

Cache:           ~/.helm/news-cache.json. Survives uvicorn restart so the
                 Home page renders instantly on first load. Background
                 refresh loop fires every news.refresh_interval_minutes.

Filters:         news.impact_filter (e.g. ['High']) + news.currency_filter
                 (e.g. ['USD']) applied at READ time so the user can flip
                 filters without forcing a refresh.

Times:           Source-supplied UTC ISO; the frontend renders in the
                 dashboard timezone. 'Today' = current CME trading day.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from fastapi import APIRouter, HTTPException

from . import settings as settings_mod
from .trading_day import (
    current_trading_day,
    trading_day_bounds_utc,
)

router = APIRouter(prefix="/api/news", tags=["news"])
logger = logging.getLogger(__name__)

from .settings import HELM_HOME  # honors HELM_HOME for isolated dev instances
CACHE_PATH      = HELM_HOME / "news-cache.json"
FF_FEED_URL     = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
ECONODAY_URL    = "https://us.econoday.com/byweek?cust=us&lid=0"
USER_AGENT      = "TheHelm/1.0 (+local dashboard; personal trade decisioning)"
FETCH_TIMEOUT_S = 20
AI_TIMEOUT_S    = 90

# Canonical set so case mismatches between sources don't double-count events.
VALID_IMPACTS   = ("High", "Medium", "Low")


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _read_cache() -> dict[str, Any]:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[news] cache read failed: %s", e)
        return {}


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError as e:
        logger.warning("[news] cache write failed: %s", e)


# ---------------------------------------------------------------------------
# Source 1: ForexFactory XML feed (no AI)
# ---------------------------------------------------------------------------

# Sample feed entry:
#   <event>
#     <title>Core CPI m/m</title>
#     <country>USD</country>
#     <date>05-29-2026</date>
#     <time>8:30am</time>
#     <impact>High</impact>
#     <forecast>0.3%</forecast>
#     <previous>0.3%</previous>
#   </event>
# Times are US/Eastern (EST/EDT) -- the feed's own README confirms this.

_FF_TZ = "America/New_York"


def _parse_ff_datetime(date_str: str, time_str: str) -> str | None:
    """Build a UTC ISO timestamp from the feed's date+time pair. Returns None
    for all-day events (time is 'All Day', 'Tentative', etc.)."""
    if not date_str or not time_str:
        return None
    t = time_str.strip()
    if not re.match(r"^\d{1,2}:\d{2}(am|pm)$", t, flags=re.IGNORECASE):
        return None
    try:
        # The feed delivers MM-DD-YYYY; assemble + parse in ET, convert to UTC.
        from zoneinfo import ZoneInfo
        naive = datetime.strptime(f"{date_str} {t}", "%m-%d-%Y %I:%M%p")
        et    = naive.replace(tzinfo=ZoneInfo(_FF_TZ))
        return et.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, ImportError) as e:
        logger.warning("[news] FF date parse failed (%s %s): %s", date_str, t, e)
        return None


def _fetch_xml(url: str, source_name: str = "forexfactory") -> tuple[list[dict[str, Any]], str | None]:
    """Fetch + XML-parse a ForexFactory-schema feed (Item 7 'xml' adapter).
    Generalizes the original _fetch_forexfactory to take a URL. A non-FF XML
    feed needs its own field mapping; v1 parses the FF schema only. Returns
    (events, error). On any failure events == []. Each event tags source_name."""
    if not url:
        return [], "xml source has no URL"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
    except requests.RequestException as e:
        return [], f"{source_name} fetch failed: {e}"

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return [], f"{source_name} XML parse failed: {e}"

    out: list[dict[str, Any]] = []
    for ev in root.findall("event"):
        title    = (ev.findtext("title") or "").strip()
        currency = (ev.findtext("country") or "").strip().upper()
        impact   = (ev.findtext("impact") or "").strip().title()
        date_s   = (ev.findtext("date") or "").strip()
        time_s   = (ev.findtext("time") or "").strip()
        forecast = (ev.findtext("forecast") or "").strip() or None
        previous = (ev.findtext("previous") or "").strip() or None
        actual   = (ev.findtext("actual") or "").strip() or None

        if not title or impact not in VALID_IMPACTS:
            continue
        time_utc = _parse_ff_datetime(date_s, time_s)
        if not time_utc:
            continue

        out.append({
            "time_utc":  time_utc,
            "currency":  currency,
            "impact":    impact,
            "title":     title,
            "source":    source_name,
            "forecast":  forecast,
            "previous":  previous,
            "actual":    actual,
        })
    return out, None


# ---------------------------------------------------------------------------
# Source: Roll Call / Factba.se presidential schedule (speaking events)
# ---------------------------------------------------------------------------

# Default CSV feed (US source: Roll Call, Washington DC; .se is a domain pun).
FACTBASE_CSV_URL = "https://media-cdn.factba.se/rss/csv/trump/calendar.csv"

# Details text that means the president is SPEAKING (vs travel/logistics).
_SPEAKING_RE = re.compile(
    r"\b(remarks?|press conference|news conference|press gaggle|gaggle|press briefing|"
    r"address(es|ing)?|speaks?|delivers?|statement|announce\w*|town hall|interview|"
    r"joint session|signing|fireside|q&a)\b", re.IGNORECASE)
# Pure logistics to drop even when a keyword trips (e.g. "lid", arrivals/departures).
_FACTBASE_SKIP_RE = re.compile(
    r"\b(lid|departs?|arrives?|en route|refuel\w*|motorcade|wheels up|wheels down)\b",
    re.IGNORECASE)


def _parse_factbase_dt(date_s: str, time_s: str) -> str | None:
    """Factba.se CSV gives ET date 'YYYY-MM-DD' + time 'HH:MM:SS' -> UTC ISO Z."""
    if not date_s or not time_s:
        return None
    try:
        from zoneinfo import ZoneInfo
        naive = datetime.strptime(f"{date_s.strip()} {time_s.strip()}", "%Y-%m-%d %H:%M:%S")
        et    = naive.replace(tzinfo=ZoneInfo(_FF_TZ))
        return et.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, ImportError):
        return None


def _fetch_factbase(url: str, source_name: str = "POTUS Schedule") -> tuple[list[dict[str, Any]], str | None]:
    """Parser-style adapter (no AI) for the presidential public-schedule CSV.

    Keeps only SPEAKING-type events (remarks / press conf / address / ...) within
    a small window around today -- the /today read-filter then narrows to the
    trading day -- and maps each to a High-impact USD news item so it surfaces
    alongside the economic calendar. CSV columns:
    Date,Time,Day,Category,Details,Location,Press Pool,Daily Summary,URL (ET times)."""
    if not url:
        return [], "factbase source has no URL"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
    except requests.RequestException as e:
        return [], f"{source_name} fetch failed: {e}"

    # Parse only rows near today so we don't load the full multi-year history.
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo(_FF_TZ)).date()
    lo, hi = today_et - timedelta(days=1), today_et + timedelta(days=3)

    out: list[dict[str, Any]] = []
    try:
        rows = csv.reader(io.StringIO(r.text))
        next(rows, None)  # header
        for row in rows:
            if len(row) < 5:
                continue
            date_s, time_s, details = row[0].strip(), row[1].strip(), row[4]
            try:
                d = datetime.strptime(date_s, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (lo <= d <= hi):
                continue
            if not _SPEAKING_RE.search(details) or _FACTBASE_SKIP_RE.search(details):
                continue
            time_utc = _parse_factbase_dt(date_s, time_s)
            if not time_utc:
                continue
            # Subject = the schedule detail; drop the trailing "[Local: ...]" and
            # the boilerplate "The President " lead-in.
            subject = re.sub(r"\s*\[Local:[^\]]*\]\s*$", "", details).strip()
            subject = re.sub(r"^The President\s+", "", subject)
            out.append({
                "time_utc":  time_utc,
                "currency":  "USD",
                "impact":    "High",
                "title":     f"POTUS: {subject}"[:140],
                "source":    source_name,
                "forecast":  None,
                "previous":  None,
                "actual":    None,
            })
    except csv.Error as e:
        return [], f"{source_name} CSV parse failed: {e}"
    return out, None


# ---------------------------------------------------------------------------
# Source 2: Econoday via AI extraction
# ---------------------------------------------------------------------------

CALENDAR_PROMPT = """You are extracting today's US economic calendar events from the HTML of {source}.

Return a JSON object with this exact shape:
{
  "events": [
    {
      "time_utc": "YYYY-MM-DDTHH:MM:SSZ",
      "currency": "USD",
      "impact": "High" | "Medium" | "Low",
      "title": "short event name",
      "forecast": null or string,
      "previous": null or string,
      "actual": null or string
    }
  ]
}

Rules:
- Only US events. Skip foreign-market entries.
- Times in the HTML are US/Eastern. Convert to UTC.
- "impact" rules: FOMC, NFP, CPI, PPI, GDP, Retail Sales, PCE, ISM, Fed Chair speak -> High.
  Jobless claims, housing, consumer confidence, Treasury auctions -> Medium. Everything else -> Low.
- If a field is unknown, use null. Never invent values.
- Return ONLY the JSON. No prose, no markdown fences.

Today's date: {today}

HTML (truncated):
{html}
"""


def _ai_extract_calendar(html: str, source_name: str = "Econoday") -> tuple[list[dict[str, Any]], str | None]:
    """Ship a calendar page's HTML to whichever AI provider is configured and
    parse its JSON output (Item 7 'scrape'/'ai-extract' adapter, generalized
    from the Econoday extractor). Returns ([], error_msg) on any failure. Each
    event tags source = source_name."""
    ai = settings_mod.get_settings().ai_backend
    # Per-component override: News can run on a different provider than signals
    # (e.g. Claude here for the large calendar HTML, Ollama for signals).
    provider = (ai.news_provider or "").strip() or ai.provider

    today_iso = datetime.now(timezone.utc).date().isoformat()
    # Trim HTML aggressively -- the calendar grid is the only useful chunk,
    # but precise selectors would need a parser. Capping at 60k chars keeps
    # cloud-API costs reasonable; an XML feed (e.g. FF) covers the must-have
    # High events so trimming a scraped source is acceptable degradation.
    #
    # str.replace, not str.format -- scraped HTML contains stray `{` / `}` in
    # inline CSS + JS, which str.format treats as positional placeholders and
    # explodes on. Bit us on 2026-05-29 with a KeyError crash every 15 min in
    # the background refresh loop. KEEP this as .replace.
    prompt = (
        CALENDAR_PROMPT
        .replace("{source}", source_name)
        .replace("{today}", today_iso)
        .replace("{html}", html[:60000])
    )

    try:
        if provider == "ollama":
            url = ai.ollama_url
            payload = {
                "model": ai.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"num_ctx": ai.num_ctx},
            }
            r = requests.post(url, json=payload, timeout=AI_TIMEOUT_S)
            r.raise_for_status()
            text = r.json().get("response", "")

        elif provider == "claude":
            if not ai.claude_api_key:
                return [], "claude provider selected but no API key"
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ai.claude_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ai.claude_model,
                    "max_tokens": ai.claude_max_tokens,
                    # No assistant prefill: claude-opus-4-8 (and other newer
                    # models) reject it with 400 "conversation must end with a
                    # user message". The prompt asks for JSON-only and the
                    # defensive parse below strips fences / extracts the {...}
                    # span, so a prefill isn't needed.
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=AI_TIMEOUT_S,
            )
            r.raise_for_status()
            blocks = r.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        elif provider == "openai":
            if not ai.openai_api_key:
                return [], "openai provider selected but no API key"
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {ai.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": ai.openai_model,
                    "max_tokens": ai.openai_max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=AI_TIMEOUT_S,
            )
            r.raise_for_status()
            choice = r.json()["choices"][0]
            text = choice["message"]["content"]
        else:
            return [], f"unknown AI provider: {provider}"

    except requests.RequestException as e:
        return [], f"AI call failed ({provider}): {e}"
    except (KeyError, IndexError, ValueError) as e:
        return [], f"AI response shape unexpected ({provider}): {e}"

    # Strip stray markdown fences if any model added them despite the prompt.
    # (No MULTILINE: anchor ^/$ to the whole string, not every line, or backticks
    # inside the JSON would get mangled.)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Model wrapped the object in prose/notes despite the prompt -- fall back
        # to the outermost {...} span.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if parsed is None:
        logger.warning("[news] Econoday AI JSON parse failed (%s); raw[:300]=%r",
                       provider, text[:300])
        return [], "AI JSON parse failed"

    events_raw = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(events_raw, list):
        return [], "AI JSON missing 'events' list"

    out: list[dict[str, Any]] = []
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        title    = (ev.get("title") or "").strip()
        impact   = (ev.get("impact") or "").strip().title()
        currency = (ev.get("currency") or "").strip().upper()
        time_utc = (ev.get("time_utc") or "").strip()
        if not title or impact not in VALID_IMPACTS or not time_utc:
            continue
        out.append({
            "time_utc":  time_utc,
            "currency":  currency or "USD",
            "impact":    impact,
            "title":     title,
            "source":    source_name,
            "forecast":  ev.get("forecast"),
            "previous":  ev.get("previous"),
            "actual":    ev.get("actual"),
        })
    return out, None


def _fetch_scrape_ai(url: str, source_name: str = "Econoday") -> tuple[list[dict[str, Any]], str | None]:
    """Fetch a calendar page's HTML and AI-extract its events (Item 7 'scrape'
    and 'ai-extract' adapter). Generalizes _fetch_econoday to take a URL +
    source name."""
    if not url:
        return [], f"{source_name} source has no URL"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
    except requests.RequestException as e:
        return [], f"{source_name} fetch failed: {e}"
    return _ai_extract_calendar(r.text, source_name)


# ---------------------------------------------------------------------------
# Merge + dedupe + filter
# ---------------------------------------------------------------------------

def _dedupe(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop near-duplicates that appear in both feeds. Key = (rounded hour,
    currency, normalized title prefix). The first seen wins; FF runs first so
    its richer fields (forecast/previous/actual) survive when both sources
    name the same release."""
    seen: dict[tuple, dict[str, Any]] = {}
    for ev in events:
        try:
            ts = datetime.fromisoformat(ev["time_utc"].replace("Z", "+00:00"))
        except ValueError:
            continue
        # Normalize title aggressively -- "Core CPI m/m" vs "CPI Core m/m"
        # vs "Core Consumer Price Index" should collapse.
        norm = re.sub(r"[^a-z0-9]+", "", ev["title"].lower())[:24]
        key  = (ts.replace(minute=0, second=0).isoformat(), ev["currency"], norm)
        if key in seen:
            # Merge: backfill any null field on the survivor from the duplicate.
            for f in ("forecast", "previous", "actual"):
                if not seen[key].get(f) and ev.get(f):
                    seen[key][f] = ev[f]
            # Track that both saw it.
            sources = set(seen[key].get("sources") or [seen[key]["source"]])
            sources.add(ev["source"])
            seen[key]["sources"] = sorted(sources)
        else:
            seen[key] = dict(ev)
            seen[key]["sources"] = [ev["source"]]
    return sorted(seen.values(), key=lambda e: e["time_utc"])


def _apply_filters(
    events: list[dict[str, Any]],
    *,
    impact_filter: list[str],
    currency_filter: list[str],
    trading_day: str,
    tz_name: str,
) -> list[dict[str, Any]]:
    """Filter the cached event list to what the Home card should show:
    only events inside the current CME trading day window, matching the
    user's impact + currency filters."""
    start_utc, end_utc = trading_day_bounds_utc(trading_day, tz_name)
    impacts = {i.title() for i in impact_filter} or set(VALID_IMPACTS)
    currencies = {c.upper() for c in currency_filter}

    out: list[dict[str, Any]] = []
    for ev in events:
        if ev["impact"] not in impacts:
            continue
        if currencies and ev["currency"] not in currencies:
            continue
        try:
            ts = datetime.fromisoformat(ev["time_utc"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (start_utc <= ts < end_utc):
            continue
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# AI reachability precheck
# ---------------------------------------------------------------------------

def _ai_reachable() -> tuple[bool, str | None]:
    """Cheap precheck used by /api/news/today so the Home card can show a
    'Configure AI to enable Econoday' hint without trying a full extraction."""
    ai = settings_mod.get_settings().ai_backend
    if ai.provider == "ollama":
        url = ai.ollama_url
        probe = url[: -len("/api/generate")] + "/api/tags" if url.endswith("/api/generate") else url
        try:
            requests.get(probe, timeout=5).raise_for_status()
            return True, None
        except requests.RequestException as e:
            return False, f"ollama unreachable at {probe}: {e}"
    if ai.provider == "claude":
        if not ai.claude_api_key:
            return False, "claude provider selected but no API key configured"
        return True, None
    if ai.provider == "openai":
        if not ai.openai_api_key:
            return False, "openai provider selected but no API key configured"
        return True, None
    return False, f"unknown AI provider: {ai.provider}"


# ---------------------------------------------------------------------------
# Refresh logic + background loop
# ---------------------------------------------------------------------------

def fetch_source(src) -> tuple[list[dict[str, Any]], str | None]:
    """Per-source parsing-adapter dispatch (Item 7). Branches on src.type:
      xml                    -> _fetch_xml (FF-schema feed, no AI)
      scrape | ai-extract    -> _fetch_scrape_ai (fetch HTML + AI-extract)
    Returns (events, error). Each event is tagged source = src.name."""
    if src.type == "xml":
        return _fetch_xml(src.url, src.name)
    if src.type == "factbase":
        return _fetch_factbase(src.url, src.name)
    # scrape / ai-extract share one code path (fetch HTML then AI-extract).
    return _fetch_scrape_ai(src.url, src.name)


def _configured_sources():
    """The enabled-or-not source list. Falls back to the two built-in defaults
    when news.sources is empty (a fresh Settings() before the migration seed
    runs, or a reset doc)."""
    cfg = settings_mod.get_settings().news
    if cfg.sources:
        return cfg.sources
    # Build transient defaults mirroring _migrate_news_sources so a fresh
    # install with no settings.json still pulls both built-ins.
    return [
        settings_mod.NewsSource(name="ForexFactory", url=FF_FEED_URL,
                                type="xml", enabled=cfg.forexfactory_enabled),
        settings_mod.NewsSource(name="Econoday", url=ECONODAY_URL,
                                type="scrape", enabled=cfg.econoday_enabled),
    ]


def _refresh_once() -> dict[str, Any]:
    """Synchronous refresh -- pulls every enabled configured source via its
    type adapter, merges, writes the cache. Returns the fresh cache payload
    (not the filtered view -- /today filters at read time so the user can flip
    filters without a refresh). One bad source records an error in the status
    map; the others still load."""
    all_events: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    ai_ok, ai_err = _ai_reachable()
    for src in _configured_sources():
        if not src.enabled:
            sources[src.name] = {"ok": False, "error": "disabled in Settings",
                                 "last_refresh": None, "count": 0}
            continue
        # scrape / ai-extract need a reachable AI backend; gate them up front.
        if src.type in ("scrape", "ai-extract") and not ai_ok:
            sources[src.name] = {"ok": False, "error": f"AI precheck: {ai_err}",
                                 "last_refresh": None, "count": 0}
            continue
        # Never let one bad source crash the whole refresh loop.
        try:
            events, err = fetch_source(src)
        except Exception as e:  # noqa: BLE001 -- isolate per-source failures
            sources[src.name] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                                 "last_refresh": None, "count": 0}
            continue
        sources[src.name] = {
            "ok": err is None, "error": err,
            "last_refresh": now_iso, "count": len(events),
        }
        all_events.extend(events)

    merged = _dedupe(all_events)
    payload = {
        "events":          merged,
        "sources":         sources,
        "fetched_at":      now_iso,
        "schema_version":  1,
    }
    _write_cache(payload)
    logger.info("[news] refreshed: %s merged=%d",
                {n: s.get("count", 0) for n, s in sources.items()}, len(merged))
    return payload


async def refresh_loop_forever() -> None:
    """Background task -- one refresh on startup, then on the user-configured
    interval. Reads the cadence each iteration so a Settings change takes
    effect on the next tick."""
    while True:
        try:
            await asyncio.to_thread(_refresh_once)
        except Exception as e:
            # Lead the log line with the exception class + message so a `grep
            # news.*ERROR` shows the cause inline without having to chase the
            # multi-line traceback that follows.
            logger.exception("[news] background refresh failed: %s: %s",
                             type(e).__name__, e)
        interval_min = max(5, settings_mod.get_settings().news.refresh_interval_minutes)
        await asyncio.sleep(interval_min * 60)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/today")
def news_today() -> dict[str, Any]:
    """Return events for the current CME trading day, filtered per the user's
    Settings → News config. Cheap: just reads the on-disk cache + filters."""
    cfg = settings_mod.get_settings()
    cache = _read_cache()
    events = cache.get("events", [])

    tz_name = cfg.appearance.timezone
    today   = current_trading_day(tz_name)

    filtered = _apply_filters(
        events,
        impact_filter=cfg.news.impact_filter,
        currency_filter=cfg.news.currency_filter,
        trading_day=today,
        tz_name=tz_name,
    )

    ai_ok, ai_err = _ai_reachable()
    # AI is required when any enabled source uses a fetch-then-AI adapter.
    ai_required = any(
        s.enabled and s.type in ("scrape", "ai-extract")
        for s in _configured_sources()
    )
    return {
        "enabled":          cfg.news.enabled,
        "trading_day":      today,
        "events":           filtered,
        "total_cached":     len(events),
        "filtered_count":   len(filtered),
        "sources":          cache.get("sources", {}),
        "fetched_at":       cache.get("fetched_at"),
        "ai_required":      ai_required,
        "ai_ok":            ai_ok,
        "ai_error":         ai_err,
        "filters": {
            "impact":    cfg.news.impact_filter,
            "currency":  cfg.news.currency_filter,
        },
    }


@router.post("/refresh")
def news_refresh() -> dict[str, Any]:
    """Force a fresh fetch from all enabled sources. Returns the new cache
    payload (unfiltered -- the GET /today route filters)."""
    if not settings_mod.get_settings().news.enabled:
        raise HTTPException(409, "news widget is disabled in Settings")
    return _refresh_once()
