"""Trading-day helpers for futures-aware date attribution.

Calendar-day attribution breaks for futures: a trade closed at 7 PM CT on
Wednesday lands on UTC Thursday, but the operator (and the exchange) treats
it as belonging to Thursday's *trading day* anyway -- the new session has
already begun.

This module exposes the trading-day primitives the rest of the dashboard
uses for "today" filters + by_day rollups. The boundary is fixed at
ROLL_HOUR (default 18 = 6 PM) in the operator's configured timezone -- so a
timestamp at or after 6 PM local belongs to the *next* trading day. DST
transitions are handled by ZoneInfo.

The exchange's actual maintenance halt is 4-5 PM CT (Mon-Thu), so 5 PM CT
is the conventional CME roll. 6 PM is a slightly later cutoff some
operators prefer to keep the late-afternoon close in the same trading day.
Change ROLL_HOUR (or surface it via Settings) if needed.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ROLL_HOUR = 18  # 6 PM in the operator's local TZ; everything at-or-after rolls to next trading day
DEFAULT_TZ = "America/Chicago"


def _tz(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def current_trading_day(tz_name: str | None = None, *,
                        now_utc: datetime | None = None,
                        roll_hour: int = ROLL_HOUR) -> str:
    """Return the current trading day as YYYY-MM-DD per the operator's TZ."""
    tz = _tz(tz_name)
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    if now_local.hour >= roll_hour:
        return (now_local.date() + timedelta(days=1)).isoformat()
    return now_local.date().isoformat()


def trading_day_for_ts(ts: str | int | float | datetime,
                       tz_name: str | None = None,
                       *,
                       roll_hour: int = ROLL_HOUR) -> str | None:
    """Map a timestamp (ISO string, unix seconds, or datetime) to its trading
    day per the operator's TZ. Returns YYYY-MM-DD, or None if the timestamp
    is unparseable."""
    tz = _tz(tz_name)
    dt: datetime | None = None
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    elif isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    if local.hour >= roll_hour:
        return (local.date() + timedelta(days=1)).isoformat()
    return local.date().isoformat()


def trading_day_bounds_utc(trading_day: str,
                           tz_name: str | None = None,
                           *,
                           roll_hour: int = ROLL_HOUR) -> tuple[datetime, datetime]:
    """Return [start, end) UTC datetimes covering one trading day.

    Trading day D spans:  [D-1 @ roll_hour local, D @ roll_hour local)
    """
    tz = _tz(tz_name)
    d = date.fromisoformat(trading_day)
    start_local = datetime.combine(d - timedelta(days=1), time(roll_hour, 0), tzinfo=tz)
    end_local   = datetime.combine(d,                     time(roll_hour, 0), tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
