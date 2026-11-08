"""Message passing between services.

Channels: ``candles`` (closed candles), ``ticks`` (throttled prices),
``forecast_requests``, ``forecasts``, ``signals``, ``alerts``, ``status``.

``MemoryBus`` runs everything in one process (development, the demo).
``RedisBus`` lets the feed, model, strategy and API services run in their own
containers.
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

CHANNELS = ("candles", "ticks", "forecast_requests", "forecasts", "signals", "alerts", "status")


class Subscription:
    def __init__(self, bus, channels, maxsize=10_000):
        self.bus = bus
        self.channels = set(channels)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _put(self, channel, msg):
        try:
            self.queue.put_nowait((channel, msg))
        except asyncio.QueueFull:
            self.dropped += 1

    async def get(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def close(self):
        await self.bus.unsubscribe(self)


class MemoryBus:
    def __init__(self):
        self.subs: list[Subscription] = []

    async def publish(self, channel: str, msg: dict):
        for s in list(self.subs):
            if channel in s.channels:
                s._put(channel, msg)

    async def subscribe(self, channels) -> Subscription:
        s = Subscription(self, channels)
        self.subs.append(s)
        return s

    async def unsubscribe(self, sub: Subscription):
        if sub in self.subs:
            self.subs.remove(sub)

    async def close(self):
        self.subs.clear()


class RedisBus:
    """Redis pub/sub. Messages are JSON."""

    def __init__(self, url: str, prefix: str = "forex:"):
        import redis.asyncio as redis  # optional dependency
        self.redis = redis.from_url(url)
        self.prefix = prefix
        self.subs: list[Subscription] = []
        self._tasks: dict[Subscription, asyncio.Task] = {}

    async def publish(self, channel: str, msg: dict):
        await self.redis.publish(self.prefix + channel, json.dumps(msg, separators=(",", ":"), default=str))

    async def subscribe(self, channels) -> Subscription:
        s = Subscription(self, channels)
        ps = self.redis.pubsub()
        await ps.subscribe(*[self.prefix + c for c in channels])

        async def pump():
            try:
                async for m in ps.listen():
                    if m.get("type") != "message":
                        continue
                    ch = m["channel"].decode() if isinstance(m["channel"], bytes) else m["channel"]
                    try:
                        s._put(ch[len(self.prefix):], json.loads(m["data"]))
                    except ValueError:
                        log.warning("bad message on %s", ch)
            except asyncio.CancelledError:
                pass
            finally:
                await ps.aclose()

        self._tasks[s] = asyncio.create_task(pump())
        self.subs.append(s)
        return s

    async def unsubscribe(self, sub: Subscription):
        t = self._tasks.pop(sub, None)
        if t:
            t.cancel()
        if sub in self.subs:
            self.subs.remove(sub)

    async def close(self):
        for s in list(self.subs):
            await self.unsubscribe(s)
        await self.redis.aclose()


def make_bus(redis_url: str | None):
    return RedisBus(redis_url) if redis_url else MemoryBus()
