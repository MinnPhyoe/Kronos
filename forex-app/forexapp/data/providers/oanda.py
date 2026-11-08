"""OANDA v20: real broker prices (streaming) and history from the same account.

Needs ``OANDA_TOKEN`` and ``OANDA_ACCOUNT`` (a practice account is fine).
Streaming: GET /v3/accounts/{id}/pricing/stream (one JSON object a line,
heartbeats every few seconds). History: GET /v3/instruments/{inst}/candles,
granularity M1, bid and ask prices, up to 5,000 candles a request.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ...timeutil import iso
from ..candles import Candle, Tick
from .base import Provider, ProviderError, parse_rfc3339

log = logging.getLogger(__name__)

HOSTS = {
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
}


def oanda_name(pair: str) -> str:
    return pair[:3] + "_" + pair[3:]


class OandaProvider(Provider):
    name = "oanda"

    def __init__(self, token: str, account: str, env: str = "practice", price_basis: str = "bid"):
        if not token or not account:
            raise ProviderError("OANDA needs OANDA_TOKEN and OANDA_ACCOUNT")
        self.api, self.stream_host = HOSTS["live" if env == "live" else "practice"]
        self.account = account
        self.headers = {"Authorization": "Bearer " + token, "Accept-Datetime-Format": "RFC3339"}
        self.price_basis = price_basis

    async def stream(self, pairs: list):
        url = "%s/v3/accounts/%s/pricing/stream" % (self.stream_host, self.account)
        params = {"instruments": ",".join(oanda_name(p) for p in pairs)}
        timeout = httpx.Timeout(10.0, read=30.0)      # heartbeats arrive every ~5 s
        async with httpx.AsyncClient(timeout=timeout, headers=self.headers) as client:
            async with client.stream("GET", url, params=params) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="replace")[:300]
                    raise ProviderError("OANDA stream HTTP %s: %s" % (r.status_code, body))
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    m = json.loads(line)
                    if m.get("type") != "PRICE" or not m.get("bids") or not m.get("asks"):
                        continue
                    yield Tick(m["instrument"].replace("_", ""), parse_rfc3339(m["time"]),
                               float(m["bids"][0]["price"]), float(m["asks"][0]["price"]))

    async def history(self, pair: str, start: int, end: int) -> list:
        url = "%s/v3/instruments/%s/candles" % (self.api, oanda_name(pair))
        out, cursor = [], start
        async with httpx.AsyncClient(timeout=30.0, headers=self.headers) as client:
            while cursor < end:
                params = {"granularity": "M1", "price": "BA", "from": iso(cursor), "count": 5000,
                          "includeFirst": "true"}
                for attempt in range(4):
                    r = await client.get(url, params=params)
                    if r.status_code == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    break
                if r.status_code != 200:
                    raise ProviderError("OANDA candles HTTP %s: %s" % (r.status_code, r.text[:300]))
                rows = r.json().get("candles", [])
                if not rows:
                    break
                last = cursor
                for row in rows:
                    t = int(parse_rfc3339(row["time"]))
                    last = max(last, t)
                    if not row.get("complete") or t >= end or t < start:
                        continue
                    b, a = row["bid"], row["ask"]
                    if self.price_basis == "mid":
                        px = {k: (float(b[k]) + float(a[k])) / 2 for k in "ohlc"}
                    else:
                        px = {k: float(b[k]) for k in "ohlc"}
                    out.append(Candle(pair, "1m", t, px["o"], px["h"], px["l"], px["c"], int(row.get("volume", 0)),
                                      max(0.0, float(a["c"]) - float(b["c"]))))
                if last <= cursor:
                    break
                cursor = last + 60
        out.sort(key=lambda c: c.open_time)
        return out
