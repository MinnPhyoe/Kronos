"""TraderMade: forex-focused WebSocket and minute time series.

Needs ``TRADERMADE_API_KEY``. Stream: wss://marketdata.tradermade.com/feedadv,
send {"userKey": KEY, "symbol": "EURUSD,GBPUSD"}; messages look like
{"symbol": "EURUSD", "ts": "1700000000123", "bid": ..., "ask": ...}.
History: /api/v1/timeseries, interval=minute, one day a request.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from ..candles import Candle, Tick
from .base import Provider, ProviderError

WS_URL = "wss://marketdata.tradermade.com/feedadv"
HIST_URL = "https://marketdata.tradermade.com/api/v1/timeseries"


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d-%H:%M")


class TraderMadeProvider(Provider):
    name = "tradermade"

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("TraderMade needs TRADERMADE_API_KEY")
        self.key = api_key

    async def stream(self, pairs: list):
        import websockets
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"userKey": self.key, "symbol": ",".join(pairs)}))
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue            # "Connected" and other text frames
                if not isinstance(m, dict) or "symbol" not in m or "bid" not in m:
                    continue
                yield Tick(m["symbol"].upper(), float(m["ts"]) / 1000.0, float(m["bid"]), float(m["ask"]))

    async def history(self, pair: str, start: int, end: int) -> list:
        out = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            t = start
            while t < end:
                t2 = min(end, t + 86400)
                r = await client.get(HIST_URL, params={"currency": pair, "api_key": self.key, "format": "records",
                                                       "interval": "minute", "period": 1,
                                                       "start_date": _fmt(t), "end_date": _fmt(t2 - 60)})
                if r.status_code != 200:
                    raise ProviderError("TraderMade timeseries HTTP %s: %s" % (r.status_code, r.text[:300]))
                for row in r.json().get("quotes", []):
                    if row.get("open") is None:
                        continue
                    ts = int(datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                    if start <= ts < end:
                        out.append(Candle(pair, "1m", ts, float(row["open"]), float(row["high"]), float(row["low"]),
                                          float(row["close"]), 0, 0.0))
                t = t2
        out.sort(key=lambda c: c.open_time)
        return out
