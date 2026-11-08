"""Model service: runs the forecaster when asked and publishes the result.

* ``forecast_requests``: the strategy service asks for a forecast for a pair
  the scanner flagged (with all raw paths in the reply).
* On every closed 5m candle (``chart_forecasts``), it also forecasts each
  pair for the chart's band, in one batch.
* Requests are coalesced: if the model is busy, only the newest request per
  pair and timeframe is kept. Kronos only runs at candle close.
* A heartbeat on ``status`` tells the API whether forecasts are available;
  the app never shows a stale forecast as live.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import Config, norm_tf
from .kronos_service import KronosUnavailable

log = logging.getLogger(__name__)


class ModelService:
    def __init__(self, cfg: Config, store, bus, forecaster):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.forecaster = forecaster
        self.pending: dict[tuple, dict] = {}
        self.wake = asyncio.Event()
        self.last_run_ms = None
        self.last_error = None
        self.ok = False
        self.runs = 0

    async def _load(self):
        if hasattr(self.forecaster, "load"):
            try:
                await asyncio.to_thread(self.forecaster.load)
            except KronosUnavailable as e:
                self.last_error = str(e)
                log.error("model unavailable: %s", e)
                await asyncio.to_thread(self.store.log_health, "model", "load_error", {"error": str(e)})
                return False
        self.ok = True
        return True

    async def _listen(self):
        sub = await self.bus.subscribe(["forecast_requests", "candles"])
        async for ch, msg in sub:
            if ch == "forecast_requests":
                key = (msg["pair"], norm_tf(msg.get("tf", "5m")), msg.get("reason", "setup"))
                self.pending[key] = msg
                self.wake.set()
            elif ch == "candles" and self.cfg.chart_forecasts and msg.get("closed") and msg.get("tf") == "5m":
                key = (msg["pair"], "5m", "chart")
                self.pending[key] = {"pair": msg["pair"], "tf": "5m", "reason": "chart",
                                     "base_time": msg["candle"]["open_time"]}
                self.wake.set()

    async def _work(self):
        while True:
            await self.wake.wait()
            await asyncio.sleep(0.05)            # let a burst of candle closes arrive together
            self.wake.clear()
            batch = list(self.pending.values())
            self.pending.clear()
            # setup requests first: they decide signals
            batch.sort(key=lambda m: m.get("reason") == "chart")
            await self._run(batch)

    async def _run(self, batch: list):
        jobs, metas = [], []
        for m in batch:
            tf = norm_tf(m.get("tf", "5m"))
            end = int(m["base_time"]) + 1 if m.get("base_time") is not None else None
            candles = await asyncio.to_thread(self.store.get_candles, m["pair"], tf, self.cfg.lookback, end)
            if not candles or (m.get("base_time") is not None and candles[-1].open_time != int(m["base_time"])):
                await self._fail(m, "candle %s not stored yet" % m.get("base_time"))
                continue
            jobs.append((candles, m["pair"], tf))
            metas.append(m)
        if not jobs:
            return
        if not self.ok and not await self._load():
            for m in metas:
                await self._fail(m, self.last_error or "model not loaded")
            return
        t0 = time.perf_counter()
        try:
            results = await asyncio.to_thread(self.forecaster.forecast_many, jobs, self.cfg.horizon, self.cfg.paths)
        except Exception as e:
            log.exception("forecast failed")
            self.last_error = str(e)
            await asyncio.to_thread(self.store.log_health, "model", "error", {"error": str(e)})
            for m in metas:
                await self._fail(m, str(e))
            return
        self.last_run_ms = (time.perf_counter() - t0) * 1000
        self.runs += 1
        await asyncio.to_thread(self.store.log_health, "model", "run",
                                {"ms": round(self.last_run_ms, 1), "jobs": len(jobs)})
        for f, m in zip(results, metas):
            full = f.to_dict(include_paths=True)
            await asyncio.to_thread(self.store.save_forecast, full)
            msg = {"type": "forecast", "request_id": m.get("id"), "reason": m.get("reason"), "ok": True,
                   "forecast": full}
            await self.bus.publish("forecasts", msg)

    async def _fail(self, m: dict, reason: str):
        await self.bus.publish("forecasts", {"type": "forecast", "request_id": m.get("id"), "reason": m.get("reason"),
                                             "ok": False, "pair": m["pair"], "tf": m.get("tf", "5m"),
                                             "error": reason})

    async def _heartbeat(self):
        while True:
            await self.bus.publish("status", {"type": "status", "service": "model", "ok": self.ok,
                                              "backend": getattr(self.forecaster, "name", "?"),
                                              "last_run_ms": self.last_run_ms, "runs": self.runs,
                                              "error": self.last_error, "ts": time.time()})
            await asyncio.sleep(5)

    async def run(self):
        await self._load()
        await asyncio.gather(self._listen(), self._work(), self._heartbeat())
