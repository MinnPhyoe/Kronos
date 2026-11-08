"""A simulated forex market for development and the demo.

Prices follow a random walk with a forex-like intraday rhythm: quiet in Asia,
busy at the London and New York opens, wider spreads at the 17:00 New York
rollover, and no prices at the weekend. It runs on its own clock so the demo
can play a morning in a few minutes (``FOREX_SIM_SPEED=30``).

It is not market data. Nothing learned from it says anything about real pairs.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import timedelta

import numpy as np

from ... import instruments
from ...timeutil import LONDON, NEW_YORK, floor_ts, local_to_utc, market_open, utc_dt
from ..candles import Candle, Tick
from .base import Provider

START_PRICES = {"EURUSD": 1.0850, "GBPUSD": 1.2900, "USDJPY": 151.20, "XAUUSD": 2650.0, "AUDUSD": 0.6600,
                "NZDUSD": 0.6000, "USDCAD": 1.3600, "USDCHF": 0.8800, "EURGBP": 0.8400, "EURJPY": 164.0,
                "GBPJPY": 195.0, "AUDJPY": 99.0, "EURCHF": 0.9500}
ANNUAL_VOL = {"XAUUSD": 0.15, "GBPJPY": 0.11, "USDJPY": 0.09, "EURJPY": 0.09, "GBPUSD": 0.08}
MINUTES_PER_YEAR = 252 * 1440


def activity(ts: float) -> float:
    """Volatility multiplier by time of day (London and New York opens are busiest)."""
    lon = utc_dt(ts).astimezone(LONDON)
    ny = utc_dt(ts).astimezone(NEW_YORK)
    m = 0.55
    lh = lon.hour + lon.minute / 60.0
    nh = ny.hour + ny.minute / 60.0
    if 7.0 <= lh < 16.5:
        m = 1.0
    if 8.0 <= lh < 10.5:
        m = 1.7
    if 8.0 <= nh < 11.0:
        m = max(m, 1.5)
    if 16.75 <= nh < 17.25:
        m = 0.4
    return m


def spread_factor(ts: float) -> float:
    ny = utc_dt(ts).astimezone(NEW_YORK)
    nh = ny.hour + ny.minute / 60.0
    return 3.0 if 16.85 <= nh < 17.35 else 1.0


class _Walk:
    """Per-pair state: price, a slow daily drift and session-open impulses."""

    def __init__(self, pair: str, rng: np.random.Generator, price: float):
        self.pair = pair
        self.ins = instruments.get(pair)
        self.rng = rng
        self.price = price
        self.sigma_min = ANNUAL_VOL.get(pair, 0.07) / math.sqrt(MINUTES_PER_YEAR)
        self.day = None
        self.day_drift = 0.0
        self.impulse_until = 0
        self.impulse = 0.0

    def _drift(self, ts: float) -> float:
        d = utc_dt(ts).date()
        if d != self.day:
            self.day = d
            self.day_drift = self.rng.normal(0.0, 0.01)      # in sigma per minute: ~0.4 daily sigma a day
        lon = utc_dt(ts).astimezone(LONDON)
        ny = utc_dt(ts).astimezone(NEW_YORK)
        # an impulse after each session's opening range, in a random direction
        for local in (lon, ny):
            if local.hour == 8 and local.minute == 15 and ts >= self.impulse_until:
                if self.rng.random() < 0.65:
                    self.impulse = self.rng.choice([-1.0, 1.0]) * self.rng.uniform(0.08, 0.25)
                    self.impulse_until = ts + 60 * int(self.rng.integers(45, 120))
        imp = self.impulse if ts < self.impulse_until else 0.0
        return self.day_drift + imp

    def minute(self, ts: int, substeps: int = 6):
        """One 1-minute candle (open, high, low, close, ticks, spread)."""
        act = activity(ts)
        sig = self.sigma_min * act / math.sqrt(substeps)
        mu = self._drift(ts) * self.sigma_min * act / substeps
        steps = self.rng.normal(mu, sig, substeps)
        path = self.price * np.exp(np.cumsum(steps))
        o = self.price
        h = max(o, float(path.max()))
        l = min(o, float(path.min()))
        c = float(path[-1])
        self.price = c
        n = int(self.rng.poisson(18 * act)) + 1
        spr = self.ins.typical_spread_pips * self.ins.pip * spread_factor(ts) * float(self.rng.uniform(0.85, 1.25))
        return o, h, l, c, n, spr

    def tick(self, ts: float, ticks_per_minute: int):
        act = activity(ts)
        sig = self.sigma_min * act / math.sqrt(ticks_per_minute)
        mu = self._drift(ts) * self.sigma_min * act / ticks_per_minute
        self.price *= math.exp(self.rng.normal(mu, sig))
        spr = self.ins.typical_spread_pips * self.ins.pip * spread_factor(ts) * float(self.rng.uniform(0.85, 1.25))
        bid = round(self.price, self.ins.digits)
        return bid, round(bid + spr, self.ins.digits)


def demo_start(now: float | None = None) -> int:
    """06:30 London on the latest weekday (Monday to Thursday) at or before now:
    the London opening range forms 90 market minutes later."""
    t = utc_dt(now if now is not None else time.time()).astimezone(LONDON)
    d = t.date()
    for _ in range(8):
        cand = local_to_utc(d, t.time().replace(hour=6, minute=30, second=0, microsecond=0), LONDON)
        if d.weekday() <= 3 and cand <= (now if now is not None else time.time()):
            return cand
        d -= timedelta(days=1)
    return floor_ts(time.time(), 60)


class SimulatedProvider(Provider):
    name = "simulated"
    realtime = False

    def __init__(self, pairs: list, speed: float = 1.0, start: int | None = None, history_days: int = 20,
                 seed: int = 7, ticks_per_minute: int = 12):
        self.pairs = [p.upper() for p in pairs]
        self.speed = max(0.01, float(speed))
        self.start = floor_ts(start if start is not None else time.time(), 60)
        self.t0_real = time.monotonic()
        self.ticks_per_minute = ticks_per_minute
        self.walks: dict[str, _Walk] = {}
        self.hist: dict[str, list] = {}
        for i, p in enumerate(self.pairs):
            rng = np.random.default_rng(seed * 1000 + i)
            w = _Walk(p, rng, START_PRICES.get(p, 1.0))
            self.walks[p] = w
            self.hist[p] = self._generate(w, self.start - history_days * 86400, self.start)

    def _generate(self, w: _Walk, start: int, end: int) -> list:
        out = []
        t = floor_ts(start, 60)
        while t < end:
            if market_open(t):
                o, h, l, c, n, s = w.minute(t)
                d = w.ins.digits
                out.append(Candle(w.pair, "1m", t, round(o, d), round(h, d), round(l, d), round(c, d), n, s))
            t += 60
        return out

    def now(self) -> float:
        return self.start + (time.monotonic() - self.t0_real) * self.speed

    async def history(self, pair: str, start: int, end: int) -> list:
        return [c for c in self.hist.get(pair, []) if start <= c.open_time < end]

    async def stream(self, pairs: list):
        step = 60.0 / self.ticks_per_minute            # market seconds between ticks
        next_t = self.now()
        while True:
            now = self.now()
            if now < next_t:
                await asyncio.sleep(max(0.0, (next_t - now) / self.speed))
                continue
            t = next_t
            next_t += step
            if not market_open(t):
                continue
            for p in pairs:
                w = self.walks.get(p)
                if w is None:
                    continue
                bid, ask = w.tick(t, self.ticks_per_minute)
                yield Tick(p, t, bid, ask)
