"""Opening-range breakout: the scanner and the entry timer (trader-scanner and
trader-entry, ported to forex).

* The opening range is the high and low of the session's first 15 minutes:
  three 5-minute candles. All three must be there, or the chart is not usable.
* The trigger is the first completed 5-minute candle after the range that
  CLOSES beyond it: above the range high for a buy, below the range low for a
  sell. A wick beyond is not a trigger, and a close exactly at the level is
  not beyond it.
* The time of the trigger is that candle's close time; its price is its close.
* One trigger per setup per day: the first close beyond. Later closes do not
  reset it.
* A setup expires if nothing triggers within ``expiry`` (90 minutes after the
  range ends by default).
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import instruments
from ..timeutil import iso
from .sessions import SessionWindow


@dataclass
class OpeningRange:
    pair: str
    session: str
    label: str
    day: str
    start: int
    end: int
    expiry: int
    high: float
    low: float
    bars: int

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def key(self) -> str:
        return "%s:%s:%s" % (self.pair, self.session, self.day)

    def to_dict(self) -> dict:
        ins = instruments.get(self.pair)
        return {"pair": self.pair, "session": self.session, "label": self.label, "day": self.day,
                "start": self.start, "end": self.end, "expiry": self.expiry, "start_iso": iso(self.start),
                "end_iso": iso(self.end), "expiry_iso": iso(self.expiry), "high": self.high, "low": self.low,
                "mid": ins.round_price(self.mid), "height_pips": round(ins.pips(self.high - self.low), 1),
                "bars": self.bars}


def opening_range(candles_5m: list, pair: str, w: SessionWindow) -> OpeningRange | None:
    """The range from the 5-minute candles inside [range_start, range_end), or None
    when a candle is missing (a gap) or the range has not finished."""
    need = (w.range_end - w.range_start) // 300
    rows = [c for c in candles_5m if w.range_start <= c.open_time < w.range_end]
    if len(rows) != need or len({c.open_time for c in rows}) != need:
        return None
    return OpeningRange(pair, w.session, w.label, w.day, w.range_start, w.range_end, w.expiry,
                        max(c.high for c in rows), min(c.low for c in rows), need)


@dataclass
class EntryState:
    status: str                  # forming | waiting | triggered | expired
    side: str | None = None
    trigger_time: int | None = None      # close time of the trigger candle
    trigger_open_time: int | None = None
    price: float | None = None           # trigger candle close
    level: float | None = None           # the range edge it closed beyond
    last_close: float | None = None
    distance_pips: float | None = None   # waiting: distance from the last close to the nearer edge

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["trigger_time_iso"] = iso(self.trigger_time)
        return d


def evaluate_entry(rng: OpeningRange, candles_5m: list, now: float) -> EntryState:
    """Waiting, triggered or expired, from candles that have CLOSED by ``now``."""
    if now < rng.end:
        return EntryState("forming")
    after = [c for c in candles_5m if c.open_time >= rng.end and c.close_time <= now]
    for c in after:
        if c.close_time > rng.expiry:
            break
        if c.close > rng.high:
            return EntryState("triggered", "buy", c.close_time, c.open_time, c.close, rng.high, c.close)
        if c.close < rng.low:
            return EntryState("triggered", "sell", c.close_time, c.open_time, c.close, rng.low, c.close)
    last = after[-1].close if after else None
    if now >= rng.expiry:
        return EntryState("expired", last_close=last)
    dist = None
    if last is not None:
        ins = instruments.get(rng.pair)
        dist = round(min(abs(rng.high - last), abs(last - rng.low)) / ins.pip, 1)
    return EntryState("waiting", last_close=last, distance_pips=dist)


def scan(candles_by_pair: dict, windows_by_pair: dict, now: float) -> list:
    """Every pair whose session range has formed: its range and entry state.
    A scan is a list of charts that match a rule, not a list of trades."""
    out = []
    for pair, wins in windows_by_pair.items():
        cs = candles_by_pair.get(pair, [])
        for w in wins:
            if now < w.range_end:
                continue
            r = opening_range(cs, pair, w)
            if r is None:
                out.append({"pair": pair, "session": w.session, "label": w.label, "usable": False,
                            "reason": "a candle is missing in the opening range"})
                continue
            st = evaluate_entry(r, cs, now)
            out.append({"pair": pair, "session": w.session, "label": w.label, "usable": True,
                        "range": r.to_dict(), "entry": st.to_dict()})
    return out
