"""EODHD: forex WebSocket (sub-50 ms) and intraday history.

Needs ``EODHD_API_KEY`` (the free demo key covers EURUSD only).
Stream: wss://ws.eodhistoricaldata.com/ws/forex?api_token=KEY, then
{"action": "subscribe", "symbols": "EURUSD,GBPUSD"}; messages look like
{"s": "EURUSD", "a": ask, "b": bid, "t": epoch_ms}.
History: /api/intraday/EURUSD.FOREX?interval=1m (about 120 days a request).
EODHD history has no spread: candles are stored with spread 0 and the
instrument's typical spread is used where a spread is needed.
"""
from __future__ import annotations

import json

import httpx

from ..candles import Candle, Tick
from .base import Provider, ProviderError

WS_URL = "wss://ws.eodhistoricaldata.com/ws/forex?api_token=%s"
HIST_URL = "https://eodhd.com/api/intraday/%s.FOREX"
CHUNK = 100 * 86400


class EodhdProvider(Provider):
    name = "eodhd"

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("EODHD needs EODHD_API_KEY")
        self.key = api_key

    async def stream(self, pairs: list):
        import websockets
        async with websockets.connect(WS_URL % self.key, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action": "subscribe", "symbols": ",".join(pairs)}))
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, dict) or "s" not in m or "b" not in m or "a" not in m:
                    continue
                yield Tick(m["s"].upper(), float(m["t"]) / 1000.0, float(m["b"]), float(m["a"]))

    async def history(self, pair: str, start: int, end: int) -> list:
        out = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            t = start
            while t < end:
                t2 = min(end, t + CHUNK)
                r = await client.get(HIST_URL % pair, params={"interval": "1m", "from": t, "to": t2,
                                                             "api_token": self.key, "fmt": "json"})
                if r.status_code != 200:
                    raise ProviderError("EODHD intraday HTTP %s: %s" % (r.status_code, r.text[:300]))
                for row in r.json():
                    ts = int(row["timestamp"])
                    if start <= ts < end and row.get("open") is not None:
                        out.append(Candle(pair, "1m", ts, float(row["open"]), float(row["high"]), float(row["low"]),
                                          float(row["close"]), int(row.get("volume") or 0), 0.0))
                t = t2
        out.sort(key=lambda c: c.open_time)
        dedup = {c.open_time: c for c in out}
        return [dedup[k] for k in sorted(dedup)]
