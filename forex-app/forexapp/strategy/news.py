"""Economic calendar and the news blackout (replaces trader-news for forex).

Stock headlines don't move EURUSD; scheduled releases do (NFP, CPI, rate
decisions). The calendar is read from a JSON feed in the ForexFactory weekly
format (``FOREX_CALENDAR_URL``; free, unofficial, can change) or a local file
(``FOREX_CALENDAR_FILE``) with the same fields:

    [{"title": "Non-Farm Employment Change", "country": "USD",
      "date": "2026-11-06T08:30:00-05:00", "impact": "High"}, ...]

No new trades from ``before`` minutes ahead of a high-impact event for
either currency of the pair until ``after`` minutes after it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from .. import instruments
from ..timeutil import iso

log = logging.getLogger(__name__)


@dataclass
class EconEvent:
    time: int
    currency: str
    title: str
    impact: str
    forecast: str = ""
    previous: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["time_iso"] = iso(self.time)
        return d


def parse_events(data) -> list:
    out = []
    for e in data or []:
        try:
            dt = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            t = int(dt.timestamp())
        except (KeyError, ValueError):
            continue
        out.append(EconEvent(t, str(e.get("country", "")).upper(), str(e.get("title", "")),
                             str(e.get("impact", "")).title(), str(e.get("forecast", "")), str(e.get("previous", ""))))
    out.sort(key=lambda x: x.time)
    return out


class Calendar:
    def __init__(self, url: str | None = None, file: str | None = None, ttl: int = 3600):
        self.url = url
        self.file = file
        self.ttl = ttl
        self.events: list = []
        self.loaded_at = 0.0
        self.error: str | None = None

    def load_file(self):
        if self.file:
            with open(self.file) as f:
                self.events = parse_events(json.load(f))
            self.loaded_at = time.time()

    async def refresh(self, force: bool = False):
        if not force and time.time() - self.loaded_at < self.ttl:
            return
        if self.url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "forex-app calendar"}) as c:
                    r = await c.get(self.url)
                    r.raise_for_status()
                    events = parse_events(r.json())
                if events:
                    self.events = events
                    self.loaded_at = time.time()
                    self.error = None
                    return
            except Exception as e:
                self.error = "calendar fetch failed: %s" % e
                log.warning(self.error)
        if self.file:
            try:
                self.load_file()
                return
            except Exception as e:
                self.error = "calendar file failed: %s" % e
        self.loaded_at = time.time()      # retry after ttl, keep the old events

    def upcoming(self, currencies: set, start: int, end: int, impacts=("High",)) -> list:
        return [e for e in self.events if start <= e.time <= end and e.currency in currencies and e.impact in impacts]


def blackout(events: list, pair: str, t: float, before_min: int = 30, after_min: int = 15,
             impacts=("High",)) -> EconEvent | None:
    """The high-impact event that blocks a new trade on ``pair`` at ``t``, if any."""
    cur = instruments.currencies(pair)
    for e in events:
        if e.currency in cur and e.impact in impacts and e.time - before_min * 60 <= t <= e.time + after_min * 60:
            return e
    return None
