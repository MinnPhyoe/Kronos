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
from .config import TIMEFRAMES, Config, UserSettings, norm_tf
from .data.candles import Candle, resample
from .models.kronos_service import KronosUnavailable, make_forecaster
from .signals.engine import path_outcomes
from .strategy import timemachine as tm
from .strategy.exits import PlanRejected, forecast_levels, plan_exit
from .strategy.indicators import atr as atr_of
from .strategy.indicators import median
from .strategy.news import parse_events
from .strategy.risk import check_order
from .strategy.scanner import evaluate_entry, opening_range
from .strategy.sessions import SESSIONS, in_quiet_window, windows_for
from .strategy.sizing import size_position
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


class AccountIn(BaseModel):
    """The account as OBSIDIAN knows it, for the Day Trader risk check (amounts in ``currency``)."""
    balance: float = Field(..., gt=0)
    currency: str = "GBP"
    risk_per_trade_pct: float = Field(0.5, gt=0, le=10)
    daily_loss_pct: float = Field(2.0, gt=0, le=50)
    max_trades_per_day: int = Field(3, ge=1)
    min_reward_risk: float = 1.0
    max_lots: float = 5.0
    max_leverage: float = 30.0
    trades_today: int = Field(0, ge=0)
    risk_used_today: float = Field(0.0, ge=0, description="max loss at the stops of today's trades")
    news_blackout_before_min: int = 30
    news_blackout_after_min: int = 15


class MarketIn(BaseModel):
    pair: str
    timeframe: str = "M15"
    candles: list[CandleIn] = Field(default_factory=list, description="closed candles of this timeframe, 64+")
    orb_candles: list[CandleIn] | None = Field(None, description="1- or 5-minute candles (2+ days) for the Day Trader setup")


class CandidateIn(BaseModel):
    """A setup OBSIDIAN's rules found, with its own risk engine's verdict."""
    source: str = "OBSIDIAN"
    name: str = "setup"
    pair: str
    timeframe: str = "M15"
    side: Literal["buy", "sell"]
    entry: float
    stop: float
    target: float
    status: str = "ready"
    pending_level: float | None = None
    obsidian_risk: Literal["PASS", "BLOCK"] | None = None
    obsidian_reasons: list[str] = Field(default_factory=list)


class BestReq(BaseModel):
    markets: list[MarketIn]
    candidates: list[CandidateIn] = Field(default_factory=list)
    include_orb: bool = True
    sessions: list[str] = ["london", "newyork"]
    account: AccountIn
    events: list[dict] = Field(default_factory=list, description="economic calendar, ForexFactory format")
    rates: dict[str, float] = Field(default_factory=dict, description="extra prices for currency conversion, e.g. GBPUSD")
    session_tz: str = "Europe/London"
    min_confidence: float = 0.55
    replays: int = Field(2000, ge=100, le=20000)
    at: float | None = None


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

    def evaluate_plan(pair: str, tf: str, cs: list, side: str, entry: float, stop: float, target: float,
                      spread_given: float | None, session_tz: str, min_conf: float, replays: int,
                      pending_level: float | None, horizon: int | None, paths: int | None) -> dict:
        """Kronos paths + the Time Machine for one plan. Raises HTTPException on bad input."""
        ins = instruments.get(pair)
        long = side == "buy"
        risk = (entry - stop) if long else (stop - entry)
        reward = (target - entry) if long else (entry - target)
        if risk <= 0 or reward <= 0:
            raise HTTPException(400, "for a %s the stop must be %s the entry and the target %s it"
                                % (side, "below" if long else "above", "above" if long else "below"))
        spread = _spread(pair, spread_given, cs)
        f, cached = forecast_for(pair, tf, cs, horizon, paths)
        po = path_outcomes(f, side, entry, stop, target, spread, pending_level)
        extreme, reach = forecast_levels(f, side, spread, cfg.stop_check_bars, cfg.stop_check_pct)
        stop_inside = (stop > extreme) if long else (stop < extreme)
        reach_ok = (reach >= target) if long else (reach <= target)
        step = TIMEFRAMES[tf]
        try:
            tz = ZoneInfo(session_tz)
        except Exception:
            raise HTTPException(400, "unknown time zone %r" % session_tz)
        last = cs[-1]
        try:
            pool = tm.build_pool(cs, tz, last.open_time + step, lookback_days=10_000, step=step)
            mc = tm.simulate(pool, side, last.close, entry, stop, target, last.open_time + step,
                             f.horizon, spread, tz, replays, cfg.tm_seed, step=step)
        except ValueError as e:
            mc = {"error": str(e)}
        conf = po["confidence"]
        filters = [
            {"name": "kronos_confidence", "ok": conf is not None and conf >= min_conf,
             "detail": ("%d of %d forecast paths reach the target before the stop (minimum %.0f%%)"
                        % (po["tp_first"], po["entered"], 100 * min_conf)) if conf is not None
             else "no forecast path reaches the entry"},
            {"name": "kronos_expectancy", "ok": po["avg_R"] is not None and po["avg_R"] > 0,
             "detail": "average forecast path result %s R" % (po["avg_R"],)},
            {"name": "timemachine", "ok": bool(mc.get("odds") is not None and mc["odds"] >= mc["break_even"]),
             "detail": mc.get("verdict") or mc.get("error")},
        ]
        return {
            "pair": pair, "timeframe": tf, "side": side, "entry": entry, "stop": stop, "target": target,
            "risk_pips": round(ins.pips(risk), 1), "reward_risk": round(reward / risk, 2), "spread": spread,
            "spread_pips": round(ins.pips(spread), 2),
            "kronos": {"model": f.model, "base_time": iso(f.base_time), "cached": cached, **po,
                       "direction_prob": round(f.direction_prob(), 4),
                       "stop_check": {"paths_10pct_extreme": round(extreme, ins.digits + 1), "stop_inside_paths": stop_inside,
                                      "window_candles": cfg.stop_check_bars},
                       "half_of_paths_reach": round(reach, ins.digits + 1), "target_within_reach": reach_ok},
            "timemachine": {k: v for k, v in mc.items() if k not in ("bands",)},
            "filters": filters, "blocks": not all(x["ok"] for x in filters),
            "reasons": [x["detail"] for x in filters if not x["ok"]], "note": NOTE}

    def orb_setups(pair: str, rows: list, sessions: list, expiry_minutes: int, stop_rule: str,
                   spread_given: float | None, at: float | None) -> tuple:
        """The Day Trader opening-range setups -> (setups, five-minute candles, now)."""
        raw = _candles(pair, "1m", rows)
        if len(raw) < 3:
            raise HTTPException(400, "send at least today's session candles")
        step = raw[1].open_time - raw[0].open_time
        five = resample(raw, "5m") if step == 60 else [Candle(pair, "5m", c.open_time, c.open, c.high, c.low, c.close,
                                                               c.tick_volume, c.spread) for c in raw]
        now = int(at) if at else five[-1].open_time + 300
        spread = _spread(pair, spread_given, five)
        ins = instruments.get(pair)
        out = []
        for w in windows_for(pair, now - 1, [s for s in sessions if s in SESSIONS], expiry_minutes):
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
                        p = plan_exit(ins, side, entry, rng.high, rng.low, atr, spread, stop_rule=stop_rule)
                        plans.append({"side": side, "entry": p.entry, "stop": p.stop, "tp1": p.tp1, "tp2": p.tp2,
                                      "stop_rule": p.stop_rule, **p.pips(ins)})
                    except PlanRejected as e:
                        plans.append({"side": side, "error": str(e)})
                item["plans"] = plans
                item["entry_window"] = [w.range_end, w.expiry]
                if es.status == "triggered" and plans and "error" not in plans[0]:
                    hit, when = first_touch(five, es.trigger_time, now, plans[0])
                    if hit:                              # already finished: not a trade any more
                        item["status"] = "closed"
                        item["outcome"] = {"result": hit, "time": when, "time_iso": iso(when)}
            out.append(item)
        return out, five, now

    @app.post("/check")
    def check(req: CheckReq, _=Depends(auth)):
        pair, tf = _pair(req.pair), _tf(req.timeframe)
        cs = _candles(pair, tf, req.candles)
        r = evaluate_plan(pair, tf, cs, req.side, req.entry, req.stop, req.target, req.spread, req.session_tz,
                          req.min_confidence, req.replays, req.pending_level, req.horizon, req.paths)
        r.pop("spread", None)
        return r

    @app.post("/orb")
    def orb(req: OrbReq, _=Depends(auth)):
        pair = _pair(req.pair)
        out, _, now = orb_setups(pair, req.candles, req.sessions, req.expiry_minutes, req.stop_rule, req.spread, req.at)
        return {"pair": pair, "at": iso(now), "setups": out,
                "note": "Day Trader opening-range breakout: a 5-minute close beyond the first 15 minutes of the session. "
                        "A setup is a rule match, not a trade; your risk engine decides."}

    @app.post("/best")
    def best(req: BestReq, _=Depends(auth)):
        return choose_best(req, evaluate_plan, orb_setups)

    return app


def first_touch(five: list, since: int | None, until: int, plan: dict) -> tuple:
    """After a breakout triggered, did price reach the target (TP1) or the stop first? -> ("target"|"stop", time)
    or (None, None). If one candle touches both, it counts as the stop: the cautious reading."""
    if since is None:
        return None, None
    buy = plan["side"] == "buy"
    for c in five:
        if c.open_time < since or c.close_time > until:
            continue
        stop = c.low <= plan["stop"] if buy else c.high >= plan["stop"]
        target = c.high >= plan["tp1"] if buy else c.low <= plan["tp1"]
        if stop:
            return "stop", c.close_time
        if target:
            return "target", c.close_time
    return None, None


def choose_best(req: "BestReq", evaluate_plan, orb_setups) -> dict:
    """Check every candidate with BOTH risk engines (OBSIDIAN's verdict, sent in, and the Day Trader
    risk check, run here), then Kronos and the Time Machine. The best is the eligible candidate with
    the highest cautious estimate: the lower of Kronos' and the Time Machine's expected R."""
    a = req.account
    st = UserSettings(account_balance=a.balance, account_currency=a.currency.upper(),
                      max_loss_per_trade=a.balance * a.risk_per_trade_pct / 100.0,
                      max_daily_loss=a.balance * a.daily_loss_pct / 100.0, max_trades_per_day=a.max_trades_per_day,
                      min_reward_risk=a.min_reward_risk, max_lots=a.max_lots, max_leverage=a.max_leverage,
                      news_blackout_before_min=a.news_blackout_before_min,
                      news_blackout_after_min=a.news_blackout_after_min)
    events = parse_events(req.events)
    today = ([{"max_loss": a.risk_used_today / a.trades_today}] * a.trades_today if a.trades_today
             else ([{"max_loss": a.risk_used_today}] if a.risk_used_today else []))
    markets, rates, latest = {}, {k.upper().replace("/", "").replace("_", ""): v for k, v in req.rates.items()}, 0
    cands = []
    for m in req.markets:
        pair, tf = _pair(m.pair), _tf(m.timeframe)
        if m.candles:
            cs = _candles(pair, tf, m.candles)
            markets[(pair, tf)] = cs
            rates.setdefault(pair, cs[-1].close)
            latest = max(latest, cs[-1].close_time)
        if req.include_orb and m.orb_candles:
            setups, five, now_orb = orb_setups(pair, m.orb_candles, req.sessions, 90, "or_mid", None, req.at)
            markets[(pair, "5m")] = five
            rates.setdefault(pair, five[-1].close)
            latest = max(latest, now_orb)
            for s in setups:
                if s.get("outcome"):                     # it already hit its target or stop
                    continue
                for p in s.get("plans", []):
                    if "error" in p:
                        continue
                    cands.append({"source": "Day Trader", "name": "%s opening-range breakout" % s["label"].replace(" ORB", ""),
                                  "pair": pair, "tf": "5m", "side": p["side"], "entry": p["entry"], "stop": p["stop"],
                                  "target": p["tp1"], "status": s["status"], "obsidian": None,
                                  "pending": (s["range"]["high"] if p["side"] == "buy" else s["range"]["low"])
                                  if s["status"] == "waiting" else None,
                                  "window": tuple(s["entry_window"]), "session": s["session"]})
    for c in req.candidates:
        cands.append({"source": c.source, "name": c.name, "pair": _pair(c.pair), "tf": _tf(c.timeframe), "side": c.side,
                      "entry": c.entry, "stop": c.stop, "target": c.target, "status": c.status,
                      "obsidian": {"verdict": c.obsidian_risk, "reasons": c.obsidian_reasons} if c.obsidian_risk else None,
                      "pending": c.pending_level, "window": None, "session": None})
    now = int(req.at) if req.at else latest
    results = []
    for c in cands:
        r = {k: c[k] for k in ("source", "name", "pair", "side", "entry", "stop", "target", "status")}
        r["timeframe"] = c["tf"]
        cs = markets.get((c["pair"], c["tf"]))
        if not cs:
            r.update(eligible=False, score_R=None, why_not=["send candles for %s %s" % (c["pair"], c["tf"])])
            results.append(r)
            continue
        try:
            ev = evaluate_plan(c["pair"], c["tf"], cs, c["side"], c["entry"], c["stop"], c["target"], None,
                               req.session_tz, req.min_confidence, req.replays, c["pending"], None, None)
        except HTTPException as e:
            r.update(eligible=False, score_R=None, why_not=[str(e.detail)])
            results.append(r)
            continue
        # risk engine 1: OBSIDIAN's own verdict (it has the final word, so it is never overridden)
        ob = c["obsidian"] or {"verdict": "NOT CHECKED", "reasons": ["OBSIDIAN's risk engine has not checked this setup yet"]}
        # risk engine 2: the Day Trader risk check, run here
        sp = [x.spread for x in cs[-60:] if x.spread > 0]
        try:
            sz = size_position(c["pair"], c["entry"], c["stop"], st, rates)
            order = {"id": "%s:%s:%s" % (c["source"], c["pair"], c["side"]), "pair": c["pair"], "side": c["side"],
                     "entry": c["entry"], "sl": c["stop"], "tp1": c["target"], "lots": sz.lots, "max_loss": sz.max_loss}
            rr = check_order(order, st, now, today_entries=today, events=events, entry_window=c["window"],
                             quiet=bool(c["session"]) and in_quiet_window(c["session"], now),
                             spread_now=cs[-1].spread if cs[-1].spread > 0 else None, spread_normal=median(sp))
            dt = {"verdict": rr.verdict, "reasons": rr.reasons, "warnings": rr.warnings, "lots": sz.lots,
                  "max_loss": sz.max_loss, "currency": st.account_currency}
        except ValueError as e:     # cannot verify (e.g. no price to convert to the account currency) -> block
            dt = {"verdict": "BLOCK", "reasons": ["cannot verify: %s" % e]}
        k, t = ev["kronos"], ev["timemachine"]
        score = round(min(k["avg_R"], t["avg_R"]), 3) if k.get("avg_R") is not None and t.get("avg_R") is not None else None
        why = []
        if ob["verdict"] != "PASS":
            why.append("OBSIDIAN risk engine: %s" % ("; ".join(ob["reasons"]) or ob["verdict"]))
        why += ["Day Trader risk check: %s" % x for x in dt["reasons"]] if dt["verdict"] != "PASS" else []
        why += ev["reasons"]
        if score is not None and score <= 0 and not ev["reasons"]:
            why.append("the more cautious estimate (%+.2fR) is not above zero" % score)
        r.update(reward_risk=ev["reward_risk"], risk_pips=ev["risk_pips"], score_R=score, eligible=not why, why_not=why,
                 checks={"obsidian_risk": ob, "daytrader_risk": dt,
                         "kronos": {"model": k["model"], "confidence": k["confidence"], "avg_R": k["avg_R"],
                                    "paths": k["paths"], "entered": k["entered"], "direction_prob": k["direction_prob"],
                                    "stop_inside_paths": k["stop_check"]["stop_inside_paths"]},
                         "timemachine": {x: t.get(x) for x in ("odds", "break_even", "avg_R", "p_target", "p_stop",
                                                               "verdict", "replays", "seed", "error")}})
        results.append(r)
    for r in results:
        r["agrees_with"] = ["%s: %s" % (o["source"], o["name"]) for o in results
                            if o is not r and o["pair"] == r["pair"] and o["side"] == r["side"] and o["source"] != r["source"]]
    ranked = sorted(results, key=lambda r: (not r["eligible"], -(r["score_R"] if r["score_R"] is not None else -99)))
    top = next((r for r in ranked if r["eligible"]), None)
    msg = ("Best: %s %s, %s (%s). It passed both risk engines, Kronos and the Time Machine; cautious estimate %+.2fR a trade."
           % (top["pair"], top["side"], top["name"], top["source"], top["score_R"])) if top else \
        "No setup passed both risk engines and the forecast checks. Waiting is a result too."
    return {"at": iso(now), "best": top, "message": msg, "ranked": ranked,
            "counts": {"candidates": len(results), "eligible": sum(r["eligible"] for r in results)},
            "how_ranked": "Only setups that pass OBSIDIAN's risk engine, the Day Trader risk check, Kronos and the Time "
                          "Machine are eligible. Among them, the highest of the lower of the two expected-R estimates wins.",
            "note": NOTE}


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
