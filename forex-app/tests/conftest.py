import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forexapp.data.candles import Candle  # noqa: E402
from forexapp.models.forecast import Forecast  # noqa: E402


def ts(text: str) -> int:
    """'2026-11-09 08:00' (UTC) -> epoch seconds."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def candle(pair, tf, t, o, h, l, c, v=10, spread=0.00008):
    return Candle(pair, tf, t, o, h, l, c, v, spread)


def flat_candles(pair, start, n, price=1.1000, step=300, wiggle=0.0002, seed=1, drift=0.0):
    """n consecutive candles around ``price`` (deterministic noise, optional drift per candle)."""
    rng = np.random.default_rng(seed)
    out, p = [], price
    for i in range(n):
        c = p + drift + rng.normal(0, wiggle / 3)
        h = max(p, c) + abs(rng.normal(0, wiggle / 4))
        l = min(p, c) - abs(rng.normal(0, wiggle / 4))
        out.append(Candle(pair, "5m" if step == 300 else "1m", start + i * step, round(p, 5), round(h, 5),
                          round(l, 5), round(c, 5), 10, 0.00008))
        p = c
    return out


def make_forecast(pair, base_time, last_close, paths, tf="5m"):
    """paths: list of per-path lists of (o, h, l, c)."""
    arr = np.asarray(paths, dtype=float)
    times = [base_time + 300 * (i + 1) for i in range(arr.shape[1])]
    return Forecast(pair, tf, base_time, last_close, times, arr, "test-model")


@pytest.fixture
def ts_():
    return ts
