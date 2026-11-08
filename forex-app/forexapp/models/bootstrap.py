"""A statistical stand-in for Kronos, for development and the demo.

It builds each path by drawing past candles from the same time of day (as the
Time Machine does) and scaling them to the current price. It has no
knowledge beyond the recent candles. Forecasts from it are labelled
``bootstrap (not Kronos)`` everywhere, and nothing it produces should be
treated as a Kronos result.
"""
from __future__ import annotations

import time
import zlib

import numpy as np

from ..config import TIMEFRAMES
from ..timeutil import next_candle_times
from .forecast import Forecast

NAME = "bootstrap (not Kronos)"


class BootstrapForecaster:
    name = NAME

    def __init__(self, window_slots: int = 2):
        self.window = window_slots

    def ready(self) -> bool:
        return True

    def forecast_many(self, jobs: list, horizon: int, n_paths: int) -> list:
        return [self.forecast(c, pair, tf, horizon, n_paths) for (c, pair, tf) in jobs]

    def forecast(self, candles: list, pair: str, tf: str, horizon: int, n_paths: int) -> Forecast:
        t0 = time.perf_counter()
        secs = TIMEFRAMES[tf]
        if len(candles) < 30:
            raise ValueError("need at least 30 candles to forecast %s %s" % (pair, tf))
        last = candles[-1]
        slots_per_day = max(1, 86400 // secs)
        # ratios of each candle to the previous close, by time-of-day slot
        pool: dict[int, list] = {}
        allr = []
        for a, b in zip(candles, candles[1:]):
            if b.open_time - a.open_time != secs:
                continue
            pc = a.close
            r = (b.open / pc, b.high / pc, b.low / pc, b.close / pc)
            pool.setdefault((b.open_time % 86400) // secs, []).append(r)
            allr.append(r)
        if not allr:
            raise ValueError("no consecutive candles to learn from")
        times = next_candle_times(last.open_time, secs, horizon)
        seed = zlib.crc32(("%s|%s|%d" % (pair, tf, last.open_time)).encode())
        rng = np.random.default_rng(seed)
        paths = np.zeros((n_paths, horizon, 4))
        for n in range(n_paths):
            p = last.close
            for j, t in enumerate(times):
                slot = (t % 86400) // secs
                cands = []
                for d in range(-self.window, self.window + 1):
                    cands.extend(pool.get((slot + d) % slots_per_day, ()))
                if len(cands) < 5:
                    cands = allr
                o, h, l, c = cands[int(rng.integers(len(cands)))]
                paths[n, j] = (p * o, p * h, p * l, p * c)
                p = p * c
        f = Forecast(pair, tf, last.open_time, last.close, times, paths, NAME)
        f.run_ms = (time.perf_counter() - t0) * 1000
        return f
