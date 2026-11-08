"""The provider interface: a live tick stream plus 1-minute history.

The feed service handles reconnects: ``stream`` may simply raise when the
connection drops, and the feed backfills the missed minutes from ``history``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import AsyncIterator

from ..candles import Candle, Tick


class ProviderError(Exception):
    pass


class Provider:
    name = "base"
    realtime = True          # False for the simulated market (it runs its own clock)

    def now(self) -> float:
        """Market clock. Real providers: the wall clock."""
        return time.time()

    async def stream(self, pairs: list) -> AsyncIterator[Tick]:  # pragma: no cover - interface
        raise NotImplementedError
        yield

    async def history(self, pair: str, start: int, end: int) -> list[Candle]:  # pragma: no cover
        """Closed 1-minute candles with start <= open_time < end, in time order."""
        raise NotImplementedError

    async def aclose(self):
        pass


def parse_rfc3339(text: str) -> float:
    """'2026-11-09T08:00:00.123456789Z' -> epoch seconds (nanoseconds trimmed)."""
    t = text.strip().replace("Z", "")
    frac = 0.0
    if "." in t:
        t, f = t.split(".", 1)
        f = "".join(ch for ch in f if ch.isdigit())
        frac = float("0." + f) if f else 0.0
    dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + frac
