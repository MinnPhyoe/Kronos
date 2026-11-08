"""Time rules. Every timestamp in the app is UTC epoch seconds (int).

Spot forex trades from Sunday 17:00 to Friday 17:00 New York time. Sessions
are defined in their own city's local time and converted to UTC here, which
handles the weeks when UK and US clocks change on different dates.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")

WEEK_CLOSE = time(17, 0)       # New York, Friday
WEEK_OPEN = time(17, 0)        # New York, Sunday


def utc_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def ts_of(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return utc_dt(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> int:
    t = text.strip().replace("Z", "+00:00")
    if " " in t and "T" not in t:
        t = t.replace(" ", "T")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return ts_of(dt)


def floor_ts(ts: float, seconds: int) -> int:
    return int(ts) - int(ts) % seconds


def local_to_utc(day: date, t: time, tz: ZoneInfo) -> int:
    return ts_of(datetime.combine(day, t, tzinfo=tz))


def market_open(ts: float) -> bool:
    """True between Sunday 17:00 and Friday 17:00 New York time."""
    ny = utc_dt(ts).astimezone(NEW_YORK)
    wd = ny.weekday()          # Monday 0 ... Sunday 6
    if wd == 5:
        return False
    if wd == 4:
        return ny.time() < WEEK_CLOSE
    if wd == 6:
        return ny.time() >= WEEK_OPEN
    return True


def week_close_utc(ts: float) -> int:
    """The Friday 17:00 New York close at or after ``ts``."""
    ny = utc_dt(ts).astimezone(NEW_YORK)
    days = (4 - ny.weekday()) % 7
    d = ny.date() + timedelta(days=days)
    close = local_to_utc(d, WEEK_CLOSE, NEW_YORK)
    if close < ts:
        close = local_to_utc(d + timedelta(days=7), WEEK_CLOSE, NEW_YORK)
    return close


def minutes_to_week_close(ts: float) -> float:
    return (week_close_utc(ts) - ts) / 60.0


def trading_day(ts: float) -> date:
    """The forex trading day: it rolls over at 17:00 New York (so Sunday evening counts as Monday)."""
    ny = utc_dt(ts).astimezone(NEW_YORK)
    return (ny + timedelta(hours=7)).date()


def next_candle_times(last_open: int, tf_seconds: int, n: int) -> list:
    """Open times of the next ``n`` candles after the one opening at ``last_open``,
    skipping the weekend close (no fake weekend candles)."""
    out, t = [], last_open
    limit = n + 3 * 86400 // tf_seconds + 10      # a weekend is at most ~2 days of candles
    for _ in range(limit):
        if len(out) >= n:
            break
        t += tf_seconds
        if market_open(t):
            out.append(t)
    return out


def is_weekend_gap(prev_open: int, next_open: int, tf_seconds: int) -> bool:
    """A gap between two candles that is fully explained by the weekend close."""
    if next_open - prev_open <= tf_seconds:
        return False
    t = prev_open + tf_seconds
    while t < next_open:
        if market_open(t):
            return False
        t += tf_seconds
    return True
