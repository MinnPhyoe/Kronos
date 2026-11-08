"""Forex session opening ranges, each defined in its own city's local time.

* London: range 08:00-08:15 London time, all year (08:00 UTC in winter, 07:00 in summer).
* New York: range 08:00-08:15 New York time. US data often lands at 08:30 New
  York, so no entries from 08:20 to 08:35 (the quiet window).
* Tokyo (optional, JPY pairs): range 09:00-09:15 Tokyo time.

The code converts each to UTC for the day, so the weeks when UK and US clocks
change on different dates come out right.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from .. import instruments
from ..timeutil import iso, local_to_utc, market_open, utc_dt


@dataclass(frozen=True)
class SessionDef:
    key: str
    label: str
    tz: ZoneInfo
    open_time: time
    range_minutes: int = 15
    currencies: frozenset | None = None          # None: every pair
    quiet: tuple = ()                            # ((start, end), ...) local times with no new entries

    def applies(self, pair: str) -> bool:
        return self.currencies is None or bool(instruments.currencies(pair) & self.currencies)


SESSIONS = {
    "london": SessionDef("london", "London ORB", ZoneInfo("Europe/London"), time(8, 0)),
    "newyork": SessionDef("newyork", "New York ORB", ZoneInfo("America/New_York"), time(8, 0),
                          quiet=((time(8, 20), time(8, 35)),)),
    "tokyo": SessionDef("tokyo", "Tokyo ORB", ZoneInfo("Asia/Tokyo"), time(9, 0), currencies=frozenset({"JPY"})),
}


@dataclass
class SessionWindow:
    session: str
    label: str
    day: str                 # the session's local date, ISO
    range_start: int         # UTC epoch seconds
    range_end: int
    expiry: int              # no trigger after this: the setup expires

    def to_dict(self) -> dict:
        return {"session": self.session, "label": self.label, "day": self.day,
                "range_start": self.range_start, "range_end": self.range_end, "expiry": self.expiry,
                "range_start_iso": iso(self.range_start), "range_end_iso": iso(self.range_end),
                "expiry_iso": iso(self.expiry)}


def window(s: SessionDef, day: date, expiry_minutes: int = 90) -> SessionWindow | None:
    """The session on a local calendar day, or None on a weekend / when the market is shut."""
    if day.weekday() >= 5:
        return None
    start = local_to_utc(day, s.open_time, s.tz)
    if not market_open(start):
        return None
    end = start + s.range_minutes * 60
    return SessionWindow(s.key, s.label, day.isoformat(), start, end, end + expiry_minutes * 60)


def window_at(s: SessionDef, ts: float, expiry_minutes: int = 90) -> SessionWindow | None:
    """The session window of the local day containing ``ts``."""
    return window(s, utc_dt(ts).astimezone(s.tz).date(), expiry_minutes)


def windows_for(pair: str, ts: float, keys: list, expiry_minutes: int = 90) -> list:
    out = []
    for k in keys:
        s = SESSIONS.get(k)
        if s is None or not s.applies(pair):
            continue
        w = window_at(s, ts, expiry_minutes)
        if w is not None:
            out.append(w)
    return out


def in_quiet_window(session_key: str, ts: float) -> bool:
    s = SESSIONS.get(session_key)
    if s is None or not s.quiet:
        return False
    lt = utc_dt(ts).astimezone(s.tz).time()
    return any(a <= lt < b for a, b in s.quiet)


def local_slot(ts: float, tz: ZoneInfo, seconds: int = 300) -> int:
    """Time-of-day slot in the session's local time (DST-aware)."""
    lt = utc_dt(ts).astimezone(tz)
    return (lt.hour * 3600 + lt.minute * 60 + lt.second) // seconds


def previous_days(day: date, n: int) -> list:
    return [day - timedelta(days=i) for i in range(1, n + 1)]
