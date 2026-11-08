"""Yahoo Finance: real prices with no account and no key (the easiest start).

Yahoo's chart endpoint is free and unofficial: it can lag a few seconds,
change, or rate-limit. Every ``poll_seconds`` (5 by default) it reads each
pair's 1-minute chart. Each minute that has finished becomes a closed
1-minute candle exactly as Yahoo has it, and the latest price becomes a tick
for the live chart. There is no bid/ask from Yahoo, so the pair's typical
spread is used.

History: Yahoo keeps 1-minute candles for about 30 days, served at most 7
days per request.

Gold: Yahoo has no spot XAUUSD, so ``XAUUSD`` reads COMEX gold futures
(GC=F), which trade a few dollars away from spot. Use OANDA for spot gold.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from ... import instruments
from ..candles import Candle, Tick
from .base import Provider, ProviderError

log = logging.getLogger(__name__)

HOSTS = ("query2", "query1")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SYMBOLS = {"USDJPY": "JPY=X", "USDCAD": "CAD=X", "USDCHF": "CHF=X", "XAUUSD": "GC=F"}
MAX_DAYS = 29              # 1-minute history Yahoo keeps
CHUNK = 6 * 86400          # under Yahoo's 7-days-per-request limit


def yahoo_symbol(pair: str) -> str:
    return SYMBOLS.get(pair, pair + "=X")


def parse_chart(text: str, pair: str) -> dict:
    """Chart JSON v8 -> {"bars": [(t, o, h, l, c, v)], "price": float|None, "time": int|None}."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise ProviderError("Yahoo returned something unexpected for %s" % pair)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise ProviderError("Yahoo has no data for %s: %s" % (pair, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        o = c if o is None else o
        h = max(o, c) if h is None else h
        lo = min(o, c) if lo is None else lo
        bars.append((int(t), float(o), float(max(h, o, c)), float(min(lo, o, c)), float(c), int(at("volume", i) or 0)))
    price = meta.get("regularMarketPrice")
    return {"bars": bars, "price": float(price) if price else None,
            "time": int(meta["regularMarketTime"]) if meta.get("regularMarketTime") else None}


class YahooProvider(Provider):
    name = "yahoo"
    bars = True               # yields closed 1-minute candles as well as ticks

    def __init__(self, poll_seconds: float = 5.0):
        self.poll = max(2.0, float(poll_seconds))
        self.client: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": UA, "Accept": "application/json"})
        return self.client

    async def fetch(self, pair: str, params: dict) -> str:
        """GET the chart: query2 first, query1 on HTTP 429 or 5xx; one retry after a pause on 429."""
        c = await self._client()
        last = "no answer"
        for host in HOSTS:
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s" % (host, yahoo_symbol(pair))
            for attempt in (0, 1):
                try:
                    r = await c.get(url, params=params)
                except httpx.HTTPError as e:
                    last = "network error: %s" % e
                    break
                if r.status_code == 200 or r.status_code == 404:
                    return r.text
                last = "HTTP %d" % r.status_code
                if r.status_code == 429 and attempt == 0:
                    await asyncio.sleep(5)
                    continue
                break
        raise ProviderError("Yahoo did not answer for %s (%s)" % (pair, last))

    def _candles(self, pair: str, bars: list, start: int, end: int) -> list:
        return [Candle(pair, "1m", t, o, h, l, c, v, 0.0) for (t, o, h, l, c, v) in bars
                if t % 60 == 0 and start <= t < end]

    async def history(self, pair: str, start: int, end: int) -> list:
        start = max(start, int(time.time()) - MAX_DAYS * 86400)
        out, t = [], start
        while t < end:
            t2 = min(end, t + CHUNK)
            text = await self.fetch(pair, {"period1": t, "period2": t2, "interval": "1m", "includePrePost": "false"})
            out += self._candles(pair, parse_chart(text, pair)["bars"], t, t2)
            t = t2
            await asyncio.sleep(0.3)          # polite
        dedup = {c.open_time: c for c in out}
        return [dedup[k] for k in sorted(dedup)]

    async def stream(self, pairs: list):
        last_bar: dict[str, int] = {}
        while True:
            t0 = time.monotonic()
            for pair in pairs:
                text = await self.fetch(pair, {"range": "1d", "interval": "1m", "includePrePost": "false"})
                d = parse_chart(text, pair)
                now = time.time()
                # only minutes that are over (Yahoo's last bar is the minute still forming)
                done = [b for b in d["bars"] if b[0] % 60 == 0 and b[0] + 60 <= now - 3]
                for c in self._candles(pair, done, last_bar.get(pair, 0) + 1, 1 << 62):
                    last_bar[pair] = c.open_time
                    yield c
                if d["price"]:
                    ins = instruments.get(pair)
                    spread = ins.typical_spread_pips * ins.pip
                    yield Tick(pair, float(d["time"] or now), d["price"], round(d["price"] + spread, ins.digits))
            await asyncio.sleep(max(0.5, self.poll - (time.monotonic() - t0)))

    async def aclose(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None
