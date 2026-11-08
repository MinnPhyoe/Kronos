"""Kronos + Day Trader engine for other apps on this computer (built for OBSIDIAN).

    python -m forexapp.engine_server            # http://127.0.0.1:8091, this computer only

OBSIDIAN's backend already fetches real candles from OANDA. It sends them here and
gets back:

| endpoint      | what it answers |
| ------------- | --------------- |
| GET  /health  | whether the model is loaded, its name, the device |
| POST /forecast| Kronos: the 10-90% band, the median path, the share of paths ending higher |
| POST /check   | for the entry, stop and target YOUR rules chose: Kronos confidence (paths hitting the target first), expected R, the stop check, and the Time Machine odds from the pair's own history |
| POST /orb     | the Day Trader opening-range breakout (London / New York / Tokyo) on 5-minute candles |

The engine never decides a trade and never places one. ``/check`` returns
``blocks``: true when the forecast argues against the plan. Use it the way OBSIDIAN
uses a lesson: it can stop a setup, never make one qualify, and the risk engine
keeps the final word. Kronos numbers are model estimates, not measured win rates:
show them as such until the Call report has graded them.

Privacy: it listens on 127.0.0.1 only. Set ``ENGINE_TOKEN`` to require
``Authorization: Bearer <token>`` as well.
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import re
import threading
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import instruments
from .config import TIMEFRAMES, Config, norm_tf
from .data.candles import Candle, resample
from .models.kronos_service import KronosUnavailable, make_forecaster
from .signals.engine import path_outcomes
from .strategy import timemachine as tm
from .strategy.exits import PlanRejected, forecast_levels, plan_exit
from .strategy.indicators import atr as atr_of
from .strategy.scanner import evaluate_entry, opening_range
from .strategy.sessions import SESSIONS, windows_for
from .timeutil import iso

log = logging.getLogger("forexapp.engine")
NOTE = ("Model estimates from Kronos and a Monte Carlo replay of this pair's own history, not measured win rates. "
        "Analysis only: the engine never places a trade.")


# ---------------------------------------------------------------- requests
class CandleIn(BaseModel):
    time: float | str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0


class ForecastReq(BaseModel):
    pair: str
    timeframe: str = "M15"
    candles: list[CandleIn] = Field(..., description="closed candles, oldest first; 64 or more (480 is ideal)")
    horizon: int | None = Field(None, ge=1, le=96)
    paths: int | None = Field(None, ge=4, le=128)


class CheckReq(ForecastReq):
    side: Literal["buy", "sell"]
    entry: float
    stop: float
    target: float
    spread: float | None = Field(None, description="in price units; default: the pair's typical spread")
    session_tz: str = "Europe/London"
    min_confidence: float = 0.55
    replays: int = Field(2000, ge=100, le=20000)
    pending_level: float | None = Field(None, description="for a limit/stop entry not yet filled: paths must reach it first")


class OrbReq(BaseModel):
    pair: str
    candles: list[CandleIn] = Field(..., description="1-minute or 5-minute candles covering today's session")
    sessions: list[str] = ["london", "newyork"]
    expiry_minutes: int = 90
    stop_rule: Literal["or_mid", "or_far"] = "or_mid"
    spread: float | None = None
    at: float | None = Field(None, description="evaluate at this time (epoch seconds); default: the last candle's close")


# ---------------------------------------------------------------- helpers
def _ts(v) -> int:
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e12 else int(v)
    s = str(v).strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*?\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)     # OANDA: ...T08:00:00.000000000Z
    if m:
        s = m.group(1) + ("." + m.group(2)[:6] if m.group(2) else "") + m.group(3)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _pair(p: str) -> str:
    s = p.upper().replace("/", "").replace("_", "")
    try:
        instruments.get(s)
    except KeyError as e:
        raise HTTPException(400, str(e))
    return s


def _candles(pair: str, tf: str, rows: list) -> list:
    out = {}
    for r in rows:
        try:
            t = _ts(r.time)
        except (ValueError, TypeError):
            raise HTTPException(400, "bad candle time %r" % (r.time,))
        if not (r.low <= min(r.open, r.close) and r.high >= max(r.open, r.close)):
            raise HTTPException(400, "candle at %s has high/low that do not contain its open/close" % iso(t))
        out[t] = Candle(pair, tf, t, r.open, r.high, r.low, r.close, int(r.volume), r.spread)
    return [out[k] for k in sorted(out)]


def _spread(pair: str, given: float | None, candles: list) -> float:
    if given is not None and given > 0:
        return given
    ins = instruments.get(pair)
    sp = [c.spread for c in candles[-60:] if c.spread > 0]
    return sorted(sp)[len(sp) // 2] if sp else ins.typical_spread_pips * ins.pip


# ---------------------------------------------------------------- the app
def create_engine_app(cfg: Config | None = None, forecaster=None, token: str | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    fc = forecaster or make_forecaster(cfg)
    token = token if token is not None else os.environ.get("ENGINE_TOKEN")
    lock = threading.Lock()
    cache: OrderedDict = OrderedDict()
    state = {"error": None, "loaded_at": None, "runs": 0, "last_ms": None}
    app = FastAPI(title="Kronos + Day Trader engine", version="1.0", description=NOTE)
    app.state.forecaster = fc

    def auth(request: Request):
        if not token:
            return
        h = request.headers.get("authorization", "")
        if not (h.lower().startswith("bearer ") and hmac.compare_digest(h[7:], token)):
            raise HTTPException(401, "missing or wrong token")

    def ensure_loaded():
        if hasattr(fc, "load") and not fc.ready():
            try:
                fc.load()
                state["loaded_at"] = time.time()
            except KronosUnavailable as e:
                state["error"] = str(e)
                raise HTTPException(503, "Kronos is not available: %s" % e)

    def forecast_for(pair: str, tf: str, candles: list, horizon: int | None, paths: int | None):
        horizon = horizon or cfg.horizon
        paths = paths or cfg.paths
        if len(candles) < 64:
            raise HTTPException(400, "need at least 64 closed candles, got %d" % len(candles))
        candles = candles[-cfg.lookback:]
        key = (getattr(fc, "name", "?"), pair, tf, candles[-1].open_time, candles[-1].close, len(candles), horizon, paths)
        with lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key], True
            ensure_loaded()
            seed = zlib.crc32(repr(key).encode())
            try:
                import torch
                torch.manual_seed(seed)                     # the same candles give the same paths
            except Exception:
                pass
            t0 = time.perf_counter()
            try:
                f = fc.forecast(candles, pair, tf, horizon, paths)
            except ValueError as e:
                raise HTTPException(400, str(e))
            state["runs"] += 1
            state["last_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            cache[key] = f
            while len(cache) > 64:
                cache.popitem(last=False)
            return f, False

    @app.get("/health")
    def health(_=Depends(auth)):
        return {"ok": state["error"] is None, "model": getattr(fc, "name", "?"),
                "loaded": bool(getattr(fc, "ready", lambda: True)()), "error": state["error"],
                "runs": state["runs"], "last_run_ms": state["last_ms"],
                "device": cfg.kronos_device or "auto", "timeframes": list(TIMEFRAMES), "note": NOTE}

    @app.post("/forecast")
    def forecast(req: ForecastReq, _=Depends(auth)):
        pair, tf = _pair(req.pair), _tf(req.timeframe)
        cs = _candles(pair, tf, req.candles)
        f, cached = forecast_for(pair, tf, cs, req.horizon, req.paths)
        d = f.to_dict()
        d.update({"cached": cached, "note": NOTE, "median_path": [round(float(x), 6) for x in f.median_path()]})
        return d

    @app.post("/check")
    def check(req: CheckReq, _=Depends(auth)):
        pair, tf = _pair(req.pair), _tf(req.timeframe)
        ins = instruments.get(pair)
        cs = _candles(pair, tf, req.candles)
        long = req.side == "buy"
        risk = (req.entry - req.stop) if long else (req.stop - req.entry)
        reward = (req.target - req.entry) if long else (req.entry - req.target)
        if risk <= 0 or reward <= 0:
            raise HTTPException(400, "for a %s the stop must be %s the entry and the target %s it"
                                % (req.side, "below" if long else "above", "above" if long else "below"))
        spread = _spread(pair, req.spread, cs)
        f, cached = forecast_for(pair, tf, cs, req.horizon, req.paths)
        po = path_outcomes(f, req.side, req.entry, req.stop, req.target, spread, req.pending_level)
        extreme, reach = forecast_levels(f, req.side, spread, cfg.stop_check_bars, cfg.stop_check_pct)
        stop_inside = (req.stop > extreme) if long else (req.stop < extreme)
        reach_ok = (reach >= req.target) if long else (reach <= req.target)
        # Time Machine from the pair's own candles (everything sent, before the last candle)
        step = TIMEFRAMES[tf]
        try:
            tz = ZoneInfo(req.session_tz)
        except Exception:
            raise HTTPException(400, "unknown time zone %r" % req.session_tz)
        last = cs[-1]
        try:
            pool = tm.build_pool(cs, tz, last.open_time + step, lookback_days=10_000, step=step)
            start_price = last.close
            mc = tm.simulate(pool, req.side, start_price, req.entry, req.stop, req.target, last.open_time + step,
                             f.horizon, spread, tz, req.replays, cfg.tm_seed, step=step)
        except ValueError as e:
            mc = {"error": str(e)}
        filters = []
        conf = po["confidence"]
        filters.append({"name": "kronos_confidence", "ok": conf is not None and conf >= req.min_confidence,
                        "detail": ("%d of %d forecast paths reach the target before the stop (minimum %.0f%%)"
                                   % (po["tp_first"], po["entered"], 100 * req.min_confidence)) if conf is not None
                        else "no forecast path reaches the entry"})
        filters.append({"name": "kronos_expectancy", "ok": po["avg_R"] is not None and po["avg_R"] > 0,
                        "detail": "average forecast path result %s R" % (po["avg_R"],)})
        filters.append({"name": "timemachine", "ok": bool(mc.get("odds") is not None and mc["odds"] >= mc["break_even"]),
                        "detail": mc.get("verdict") or mc.get("error")})
        return {
            "pair": pair, "timeframe": tf, "side": req.side, "entry": req.entry, "stop": req.stop, "target": req.target,
            "risk_pips": round(ins.pips(risk), 1), "reward_risk": round(reward / risk, 2), "spread_pips": round(ins.pips(spread), 2),
            "kronos": {"model": f.model, "base_time": iso(f.base_time), "cached": cached, **po,
                       "direction_prob": round(f.direction_prob(), 4),
                       "stop_check": {"paths_10pct_extreme": round(extreme, ins.digits + 1), "stop_inside_paths": stop_inside,
                                      "window_candles": cfg.stop_check_bars},
                       "half_of_paths_reach": round(reach, ins.digits + 1), "target_within_reach": reach_ok},
            "timemachine": {k: v for k, v in mc.items() if k not in ("bands",)},
            "filters": filters, "blocks": not all(x["ok"] for x in filters),
            "reasons": [x["detail"] for x in filters if not x["ok"]], "note": NOTE}

    @app.post("/orb")
    def orb(req: OrbReq, _=Depends(auth)):
        pair = _pair(req.pair)
        raw = _candles(pair, "1m", req.candles)
        if len(raw) < 3:
            raise HTTPException(400, "send at least today's session candles")
        step = raw[1].open_time - raw[0].open_time
        five = resample(raw, "5m") if step == 60 else [Candle(pair, "5m", c.open_time, c.open, c.high, c.low, c.close,
                                                               c.tick_volume, c.spread) for c in raw]
        now = int(req.at) if req.at else five[-1].open_time + 300
        spread = _spread(pair, req.spread, five)
        ins = instruments.get(pair)
        out = []
        for w in windows_for(pair, now - 1, [s for s in req.sessions if s in SESSIONS], req.expiry_minutes):
            item = {"session": w.session, "label": w.label, "window": w.to_dict()}
            if now < w.range_end:
                item["status"] = "forming" if now >= w.range_start else "not started"
                out.append(item)
                continue
            rng = opening_range(five, pair, w)
            if rng is None:
                item.update(status="unusable", reason="a 5-minute candle is missing in the opening range")
                out.append(item)
                continue
            es = evaluate_entry(rng, five, now)
            item.update(status=es.status, range=rng.to_dict(), entry_state=es.to_dict())
            if es.status in ("waiting", "triggered"):
                plans = []
                sides = [(es.side, es.price)] if es.status == "triggered" else [("buy", rng.high), ("sell", rng.low)]
                atr = atr_of([c for c in five if c.close_time <= now], 14)
                for side, level in sides:
                    entry = level + spread if side == "buy" else level
                    try:
                        p = plan_exit(ins, side, entry, rng.high, rng.low, atr, spread, stop_rule=req.stop_rule)
                        plans.append({"side": side, "entry": p.entry, "stop": p.stop, "tp1": p.tp1, "tp2": p.tp2,
                                      "stop_rule": p.stop_rule, **p.pips(ins)})
                    except PlanRejected as e:
                        plans.append({"side": side, "error": str(e)})
                item["plans"] = plans
            out.append(item)
        return {"pair": pair, "at": iso(now), "setups": out,
                "note": "Day Trader opening-range breakout: a 5-minute close beyond the first 15 minutes of the session. "
                        "A setup is a rule match, not a trade; your risk engine decides."}

    return app


def _tf(tf: str) -> str:
    try:
        return norm_tf(tf)
    except ValueError as e:
        raise HTTPException(400, str(e))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.engine_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("ENGINE_PORT", 8091)))
    ap.add_argument("--host", default=os.environ.get("ENGINE_HOST", "127.0.0.1"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn
    app = create_engine_app()
    print("\n  Kronos + Day Trader engine on http://%s:%d  (model: %s; it loads on the first request)\n"
          % (a.host, a.port, getattr(app.state.forecaster, "name", "?")), flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
