"""Candles, the live candle builder and timeframe aggregation.

Candle rules (fixed once, used for training, backtesting and live signals):

* All timestamps are UTC epoch seconds.
* A candle belongs to the period it starts: the 08:00 one-minute candle covers
  08:00:00 to 08:00:59.
* Prices are bid (or mid, by config). The spread is stored separately, as the
  average spread of the ticks in the candle, in price units.
* Volume is the tick count (spot forex has no central volume).
* Gaps are never filled with fake candles.
* 5m, 15m and 1H candles are built from the 1-minute candles, so every
  timeframe follows the same rules.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass

from ..config import HIGHER_TIMEFRAMES, TIMEFRAMES
from ..timeutil import floor_ts, iso

log = logging.getLogger(__name__)


@dataclass
class Tick:
    pair: str
    time: float          # epoch seconds, UTC
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass
class Candle:
    pair: str
    tf: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: float = 0.0

    @property
    def close_time(self) -> int:
        return self.open_time + TIMEFRAMES[self.tf]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = iso(self.open_time)
        return d

    def to_row(self) -> list:
        return [self.open_time, self.open, self.high, self.low, self.close, self.tick_volume, self.spread]

    @classmethod
    def from_dict(cls, d: dict) -> "Candle":
        return cls(d["pair"], d["tf"], int(d["open_time"]), float(d["open"]), float(d["high"]),
                   float(d["low"]), float(d["close"]), int(d.get("tick_volume") or 0),
                   float(d.get("spread") or 0.0))


class _Forming:
    __slots__ = ("open_time", "o", "h", "l", "c", "n", "spread_sum")

    def __init__(self, open_time, price, spread):
        self.open_time = open_time
        self.o = self.h = self.l = self.c = price
        self.n = 1
        self.spread_sum = spread

    def add(self, price, spread):
        self.h = max(self.h, price)
        self.l = min(self.l, price)
        self.c = price
        self.n += 1
        self.spread_sum += spread

    def candle(self, pair) -> Candle:
        return Candle(pair, "1m", self.open_time, self.o, self.h, self.l, self.c, self.n,
                      self.spread_sum / self.n)


class CandleBuilder:
    """Turns ticks into closed 1-minute candles.

    ``on_tick`` returns the candles a tick closed (the previous minute, once a
    tick of a later minute arrives). ``flush(now)`` closes a minute on time
    even when no later tick arrives (a quiet market), a little after it ends.
    """

    def __init__(self, price_basis: str = "bid", grace_seconds: float = 2.0):
        if price_basis not in ("bid", "mid"):
            raise ValueError("price_basis must be bid or mid")
        self.price_basis = price_basis
        self.grace = grace_seconds
        self.forming: dict[str, _Forming] = {}
        self.last_closed: dict[str, int] = {}
        self.late_ticks = 0

    def set_last_closed(self, pair: str, open_time: int):
        """After a backfill: ignore ticks for minutes that are already stored."""
        self.last_closed[pair] = max(open_time, self.last_closed.get(pair, 0))

    def _price(self, t: Tick) -> float:
        return t.bid if self.price_basis == "bid" else t.mid

    def on_tick(self, tick: Tick) -> list:
        minute = floor_ts(tick.time, 60)
        if minute <= self.last_closed.get(tick.pair, -1):
            self.late_ticks += 1
            return []
        f = self.forming.get(tick.pair)
        out = []
        if f is not None and minute > f.open_time:
            out.append(self._close(tick.pair))
            f = None
        elif f is not None and minute < f.open_time:
            self.late_ticks += 1
            return []
        price = self._price(tick)
        if f is None:
            self.forming[tick.pair] = _Forming(minute, price, tick.spread)
        else:
            f.add(price, tick.spread)
        return out

    def flush(self, now: float) -> list:
        out = []
        for pair, f in list(self.forming.items()):
            if now >= f.open_time + 60 + self.grace:
                out.append(self._close(pair))
        return out

    def forming_candle(self, pair: str) -> Candle | None:
        f = self.forming.get(pair)
        return f.candle(pair) if f else None

    def _close(self, pair: str) -> Candle:
        f = self.forming.pop(pair)
        self.last_closed[pair] = f.open_time
        return f.candle(pair)


def merge(candles: list, pair: str, tf: str, open_time: int) -> Candle:
    """One candle of timeframe ``tf`` from the smaller candles inside it (in order)."""
    n = sum(c.tick_volume for c in candles)
    spread = (sum(c.spread * max(c.tick_volume, 1) for c in candles) /
              max(1, sum(max(c.tick_volume, 1) for c in candles)))
    return Candle(pair, tf, open_time, candles[0].open, max(c.high for c in candles),
                  min(c.low for c in candles), candles[-1].close, n, spread)


def resample(candles_1m: list, tf: str) -> list:
    """Build ``tf`` candles from 1-minute candles (sorted by time). A bucket with
    any 1-minute candle becomes a candle; empty buckets (the weekend) stay empty."""
    secs = TIMEFRAMES[tf]
    out, bucket, cur = [], [], None
    for c in candles_1m:
        b = floor_ts(c.open_time, secs)
        if cur is not None and b != cur:
            out.append(merge(bucket, c.pair, tf, cur))
            bucket = []
        cur = b
        bucket.append(c)
    if bucket:
        out.append(merge(bucket, bucket[0].pair, tf, cur))
    return out


class Aggregator:
    """Builds 5m, 15m and 1H candles from closed 1-minute candles, live.

    A higher candle closes when its last minute closes, or, if that minute had
    no ticks, when the first minute of a later bucket closes.
    """

    def __init__(self, timeframes=HIGHER_TIMEFRAMES, keep_minutes: int = 120):
        self.timeframes = tuple(timeframes)
        self.minutes: dict[str, deque] = {}
        self.keep = keep_minutes
        self.open_bucket: dict[tuple, int] = {}

    def seed(self, candles_1m: list):
        """Load recent 1-minute candles (e.g. after a restart) without emitting."""
        for c in candles_1m:
            dq = self.minutes.setdefault(c.pair, deque(maxlen=self.keep))
            dq.append(c)
            for tf in self.timeframes:
                self.open_bucket[(c.pair, tf)] = floor_ts(c.open_time, TIMEFRAMES[tf])
        # the last bucket may already be complete
        for c in candles_1m[-1:]:
            for tf in self.timeframes:
                b = floor_ts(c.open_time, TIMEFRAMES[tf])
                if c.close_time == b + TIMEFRAMES[tf]:
                    self.open_bucket.pop((c.pair, tf), None)

    def on_1m_close(self, c: Candle) -> list:
        dq = self.minutes.setdefault(c.pair, deque(maxlen=self.keep))
        if dq and c.open_time <= dq[-1].open_time:
            return []
        out = []
        for tf in self.timeframes:
            secs = TIMEFRAMES[tf]
            b = floor_ts(c.open_time, secs)
            key = (c.pair, tf)
            prev = self.open_bucket.get(key)
            if prev is not None and prev != b:
                # the previous bucket ended without its last minute: close it now
                rows = [m for m in dq if floor_ts(m.open_time, secs) == prev]
                if rows:
                    out.append(merge(rows, c.pair, tf, prev))
                self.open_bucket.pop(key, None)
        dq.append(c)
        for tf in self.timeframes:
            secs = TIMEFRAMES[tf]
            b = floor_ts(c.open_time, secs)
            key = (c.pair, tf)
            if c.close_time == b + secs:
                rows = [m for m in dq if floor_ts(m.open_time, secs) == b]
                out.append(merge(rows, c.pair, tf, b))
                self.open_bucket.pop(key, None)
            else:
                self.open_bucket[key] = b
        return out

    def forming(self, pair: str, tf: str, live_1m: Candle | None = None) -> Candle | None:
        secs = TIMEFRAMES[tf]
        b = self.open_bucket.get((pair, tf))
        rows = [m for m in self.minutes.get(pair, ()) if b is not None and floor_ts(m.open_time, secs) == b]
        if live_1m is not None:
            lb = floor_ts(live_1m.open_time, secs)
            if b is None or lb == b:
                rows = rows + [live_1m]
                b = lb
        if not rows:
            return None
        return merge(rows, pair, tf, b)
