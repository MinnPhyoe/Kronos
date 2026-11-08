"""Feed service: provider stream -> 1-minute candles -> 5m/15m/1H -> store + bus.

1. Connects to the provider and reconnects (with backoff) when it drops.
2. Each tick updates the forming 1-minute candle.
3. When the minute ends, the closed candle is saved and a ``candle`` message
   (closed=True) is published.
4. Higher timeframes are rebuilt from the 1-minute candles on each close.
5. On (re)connect it backfills missed minutes from the provider's history.
Ticks go to the app at most a few times a second per pair.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import HIGHER_TIMEFRAMES
from .candles import Aggregator, Candle, CandleBuilder, resample
from .providers.base import Provider

log = logging.getLogger(__name__)

TICK_INTERVAL = 0.25          # seconds between tick messages per pair


class FeedService:
    def __init__(self, provider: Provider, store, bus, pairs: list, price_basis: str = "bid",
                 backfill_days: int = 3):
        self.provider = provider
        self.store = store
        self.bus = bus
        self.pairs = list(pairs)
        self.builder = CandleBuilder(price_basis)
        self.agg = Aggregator()
        self.backfill_days = backfill_days
        self.last_tick_sent: dict[str, float] = {}
        self.last_tick: dict = {}
        self.connected = False
        self.disconnects = 0
        self.last_candle_time: dict[str, int] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ history
    async def backfill(self):
        """Fill missing 1-minute candles since the last stored one (or seed history),
        then rebuild the higher timeframes for the filled range."""
        now = self.provider.now()
        end = int(now) - int(now) % 60                     # only minutes that are over
        for pair in self.pairs:
            last = await asyncio.to_thread(self.store.last_candle_time, pair, "1m")
            start = (last + 60) if last else end - self.backfill_days * 86400
            start = max(start, end - self.backfill_days * 86400) if last else start
            if start >= end:
                self._seed_state(pair)
                continue
            try:
                rows = await self.provider.history(pair, start, end)
            except Exception as e:
                log.warning("backfill %s failed: %s", pair, e)
                await asyncio.to_thread(self.store.log_health, "feed", "backfill_error", {"pair": pair, "error": str(e)})
                self._seed_state(pair)
                continue
            if rows:
                await asyncio.to_thread(self.store.upsert_candles, rows)
                # rebuild higher candles over the filled range (from whole buckets)
                lo = rows[0].open_time - rows[0].open_time % 3600
                base = await asyncio.to_thread(self.store.get_candles, pair, "1m", None, end, lo)
                for tf in HIGHER_TIMEFRAMES:
                    hc = resample(base, tf)
                    # the last bucket may still be forming: keep only buckets that are over
                    hc = [c for c in hc if c.close_time <= end]
                    await asyncio.to_thread(self.store.upsert_candles, hc)
                log.info("backfilled %s: %d 1m candles", pair, len(rows))
                await asyncio.to_thread(self.store.log_health, "feed", "backfill", {"pair": pair, "candles": len(rows)})
            self._seed_state(pair)

    def _seed_state(self, pair):
        recent = self.store.get_candles(pair, "1m", limit=120)
        if recent:
            self.builder.set_last_closed(pair, recent[-1].open_time)
            self.agg.seed(recent)
            self.last_candle_time[pair] = recent[-1].open_time

    # ------------------------------------------------------------------ live
    async def _closed(self, c: Candle):
        await asyncio.to_thread(self.store.upsert_candles, [c])
        self.last_candle_time[c.pair] = c.open_time
        await self.bus.publish("candles", {"type": "candle", "pair": c.pair, "tf": "1m", "closed": True,
                                           "candle": c.to_dict()})
        higher = self.agg.on_1m_close(c)
        if higher:
            await asyncio.to_thread(self.store.upsert_candles, higher)
            for h in higher:
                await self.bus.publish("candles", {"type": "candle", "pair": h.pair, "tf": h.tf, "closed": True,
                                                   "candle": h.to_dict()})

    async def _flusher(self):
        while not self._stop.is_set():
            for c in self.builder.flush(self.provider.now()):
                await self._closed(c)
            await asyncio.sleep(0.5 if self.provider.realtime else 0.1)

    async def _heartbeat(self):
        while not self._stop.is_set():
            await self.bus.publish("status", {"type": "status", "service": "feed", "ok": self.connected,
                                              "provider": self.provider.name, "market_time": self.provider.now(),
                                              "speed": getattr(self.provider, "speed", 1.0),
                                              "disconnects": self.disconnects, "late_ticks": self.builder.late_ticks,
                                              "last_candle": self.last_candle_time, "ts": time.time()})
            await asyncio.sleep(5)

    async def run(self):
        await self.backfill()
        tasks = [asyncio.create_task(self._flusher()), asyncio.create_task(self._heartbeat())]
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    self.connected = True
                    await self.bus.publish("status", {"type": "status", "service": "feed", "ok": True,
                                                      "event": "connected", "ts": time.time()})
                    async for item in self.provider.stream(self.pairs):
                        backoff = 1.0
                        if isinstance(item, Candle):
                            # a provider that serves finished 1-minute candles itself (Yahoo)
                            if item.open_time > self.last_candle_time.get(item.pair, -1):
                                await self._closed(item)
                            continue
                        tick = item
                        if not getattr(self.provider, "bars", False):
                            for c in self.builder.on_tick(tick):
                                await self._closed(c)
                        self.last_tick[tick.pair] = tick
                        now = time.monotonic()
                        if now - self.last_tick_sent.get(tick.pair, 0) >= TICK_INTERVAL:
                            self.last_tick_sent[tick.pair] = now
                            await self.bus.publish("ticks", {"type": "tick", "pair": tick.pair, "time": tick.time,
                                                             "bid": tick.bid, "ask": tick.ask})
                        if self._stop.is_set():
                            break
                    if self._stop.is_set():
                        break
                    raise ConnectionError("stream ended")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.connected = False
                    self.disconnects += 1
                    log.warning("feed disconnected (%s); reconnecting in %.0fs", e, backoff)
                    await asyncio.to_thread(self.store.log_health, "feed", "disconnect", {"error": str(e)})
                    await self.bus.publish("status", {"type": "status", "service": "feed", "ok": False,
                                                      "event": "disconnected", "error": str(e), "ts": time.time()})
                    await asyncio.sleep(backoff)
                    backoff = min(60.0, backoff * 2)
                    await self.backfill()
        finally:
            self._stop.set()
            for t in tasks:
                t.cancel()

    def stop(self):
        self._stop.set()

    def latest_prices(self) -> dict:
        return {p: t.bid for p, t in self.last_tick.items()}
