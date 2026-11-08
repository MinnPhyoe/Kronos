"""A small client for the Kronos + Day Trader engine. Standard library only, so OBSIDIAN's
backend can copy this one file in without new dependencies.

    from engine_client import EngineClient, from_oanda
    eng = EngineClient()                                  # http://127.0.0.1:8091
    candles = from_oanda(oanda_json["candles"])           # OANDA /candles answer -> engine candles
    band = eng.forecast("EUR_USD", "M15", candles)
    check = eng.check("EUR_USD", "M15", candles, side="buy", entry=1.0852, stop=1.0838, target=1.0866)
    if check["blocks"]:
        ...  # treat like a lesson: it can stop the setup, never make it qualify
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class EngineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__("engine HTTP %s: %s" % (status, detail))
        self.status, self.detail = status, detail


def from_oanda(candles: list, price: str = "mid", complete_only: bool = True) -> list:
    """OANDA v20 candles -> engine candles. ``price``: "mid", "bid" or "ask" (whichever you requested).
    The spread is filled in when both bid and ask are present."""
    out = []
    for c in candles:
        if complete_only and not c.get("complete", True):
            continue
        p = c.get(price) or c.get("mid") or c.get("bid")
        if not p:
            continue
        row = {"time": c["time"], "open": float(p["o"]), "high": float(p["h"]), "low": float(p["l"]),
               "close": float(p["c"]), "volume": float(c.get("volume", 0))}
        if c.get("bid") and c.get("ask"):
            row["spread"] = max(0.0, float(c["ask"]["c"]) - float(c["bid"]["c"]))
        out.append(row)
    return out


class EngineClient:
    def __init__(self, url: str = "http://127.0.0.1:8091", token: str | None = None, timeout: float = 180.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("detail", e.reason)
            except Exception:
                detail = e.reason
            raise EngineError(e.code, str(detail))
        except urllib.error.URLError as e:
            raise EngineError(0, "engine not reachable (%s); is `python -m forexapp.engine_server` running?" % e.reason)

    def health(self) -> dict:
        return self._call("GET", "/health")

    def forecast(self, pair: str, timeframe: str, candles: list, horizon: int | None = None, paths: int | None = None) -> dict:
        return self._call("POST", "/forecast", {"pair": pair, "timeframe": timeframe, "candles": candles,
                                                "horizon": horizon, "paths": paths})

    def check(self, pair: str, timeframe: str, candles: list, side: str, entry: float, stop: float, target: float,
              **kw) -> dict:
        body = {"pair": pair, "timeframe": timeframe, "candles": candles, "side": side, "entry": entry,
                "stop": stop, "target": target}
        body.update({k: v for k, v in kw.items() if v is not None})
        return self._call("POST", "/check", body)

    def best(self, markets: list, candidates: list, account: dict, **kw) -> dict:
        """Check every setup with both risk engines, Kronos and the Time Machine; return the best.
        markets: [{"pair", "timeframe", "candles", "orb_candles"?}]
        candidates: OBSIDIAN's setups, each with "obsidian_risk": "PASS" or "BLOCK" from its risk engine
        account: {"balance", "currency", "risk_per_trade_pct", "daily_loss_pct", "trades_today", "risk_used_today"}"""
        body = {"markets": markets, "candidates": candidates, "account": account}
        body.update({k: v for k, v in kw.items() if v is not None})
        return self._call("POST", "/best", body)

    def orb(self, pair: str, candles: list, **kw) -> dict:
        body = {"pair": pair, "candles": candles}
        body.update({k: v for k, v in kw.items() if v is not None})
        return self._call("POST", "/orb", body)
