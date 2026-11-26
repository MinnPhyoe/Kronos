"""API service (FastAPI): REST for history plus a WebSocket for live updates.

| endpoint                                | returns |
| --------------------------------------- | ------- |
| GET /candles?pair=EURUSD&tf=5m&limit=500 | candle history for the chart |
| GET /forecast?pair=EURUSD&tf=5m          | latest forecast band (p10, p50, p90) |
| GET /signals?status=active               | current signals |
| GET /signals/history                     | past signals and their outcomes |
| POST /signals/{id}/action                | "took" or "skipped" (feeds the results log) |
| WS /stream                               | live pushes: candle, tick, forecast, signal, alert, status |
| GET /sessions, /scan, /calendar          | session ranges, scanner, economic calendar |
| GET /stats, /monitor                     | results per setup, the weekly monitor |
| GET/PUT /settings, GET /health, /pairs   | |

Rules: the provider key stays on the server; ticks go out a few times a
second, forecasts and signals once per candle close; if the model is down the
candles keep streaming and signals are marked unavailable. A forecast older
than the last closed candle is flagged ``stale`` and never shown as live.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import instruments
from ..config import APP_DIR, TIMEFRAMES, Config, UserSettings, norm_tf
from ..strategy.news import Calendar
from ..strategy.scanner import opening_range, scan
from ..strategy.sessions import windows_for
from ..timeutil import iso
from .auth import http_user, ws_user

log = logging.getLogger(__name__)
STATUS_TTL = 20.0       # seconds without a heartbeat before a service counts as down


class ActionBody(BaseModel):
    action: str


class Health:
    def __init__(self):
        self.services: dict[str, dict] = {}

    def update(self, msg: dict):
        svc = msg.get("service")
        if svc:
            cur = self.services.setdefault(svc, {})
            cur.update({k: v for k, v in msg.items() if k not in ("type",)})
            cur["seen"] = time.time()

    def up(self, svc: str) -> bool:
        s = self.services.get(svc)
        return bool(s and s.get("ok") and time.time() - s.get("seen", 0) < STATUS_TTL)

    def market_time(self) -> float | None:
        s = self.services.get("feed") or {}
        if s.get("market_time") and time.time() - s.get("seen", 0) < STATUS_TTL:
            return float(s["market_time"]) + (time.time() - s["seen"]) * s.get("speed", 1.0)
        return None


def _slim_forecast(f: dict) -> dict:
    return {k: v for k, v in f.items() if k not in ("paths", "times")}


def create_app(cfg: Config, store, bus, calendar: Calendar | None = None, health: Health | None = None,
               forecaster=None, engine: bool = True) -> FastAPI:
    health = health or Health()

    async def _status_listener():
        sub = await bus.subscribe(["status"])
        async for _, msg in sub:
            health.update(msg)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_status_listener())
        yield
        task.cancel()

    app = FastAPI(title="Forex Analysis API", version="1.0", lifespan=lifespan,
                  description="Signals are analysis and education, not financial advice.")
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"])
    calendar = calendar or Calendar(cfg.calendar_url, cfg.calendar_file)
    user = http_user(cfg)
    app.state.health = health
    app.state.store = store

    def _settings(uid: str) -> UserSettings:
        return UserSettings.from_dict(store.get_settings(uid) or store.get_settings("default") or
                                      {"pairs": cfg.pairs, "sessions": cfg.sessions})

    def _pair(pair: str) -> str:
        p = pair.upper().replace("/", "")
        try:
            instruments.get(p)
        except KeyError as e:
            raise HTTPException(400, str(e))
        return p

    def _tf(tf: str) -> str:
        try:
            return norm_tf(tf)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ market data
    @app.get("/pairs")
    def pairs(uid: str = Depends(user)):
        st = _settings(uid)
        out = []
        for p in st.pairs:
            try:
                ins = instruments.get(p)
            except KeyError:
                continue
            out.append({"pair": p, "base": ins.base, "quote": ins.quote, "pip": ins.pip, "digits": ins.digits})
        return {"pairs": out, "timeframes": list(TIMEFRAMES)}

    @app.get("/candles")
    def candles(pair: str = "EURUSD", tf: str = "5m", limit: int = Query(500, ge=1, le=5000),
                end: int | None = None, uid: str = Depends(user)):
        p, t = _pair(pair), _tf(tf)
        if t in ("1m", "5m", "15m", "1h"):
            rows = store.get_candles(p, t, limit=limit, end=end)
        else:                                   # 30m comes from 15m, 2h / 4h / 1d from 1h, built on request
            from ..data.candles import resample
            src = "15m" if TIMEFRAMES[t] < 3600 else "1h"
            per = TIMEFRAMES[t] // TIMEFRAMES[src]
            base = store.get_candles(p, src, limit=min(limit * per + per, 60000), end=end)
            rows = resample(base, t)[-limit:]
        return {"pair": p, "tf": t, "candles": [[c.open_time, c.open, c.high, c.low, c.close, c.tick_volume,
                                                 round(c.spread, 8)] for c in rows],
                "columns": ["time", "open", "high", "low", "close", "tick_volume", "spread"]}

    @app.get("/forecast")
    def forecast(pair: str = "EURUSD", tf: str = "5m", uid: str = Depends(user)):
        p, t = _pair(pair), _tf(tf)
        f = store.latest_forecast(p, t)
        last = store.last_candle_time(p, t)
        if f is None:
            return {"pair": p, "tf": t, "available": False, "reason": "no forecast yet",
                    "model_up": health.up("model")}
        out = _slim_forecast(f)
        out["stale"] = bool(last is not None and f["base_time"] < last)
        out["available"] = not out["stale"]
        out["model_up"] = health.up("model")
        return out

    @app.get("/sessions")
    def sessions(pair: str = "EURUSD", at: int | None = None, uid: str = Depends(user)):
        p = _pair(pair)
        st = _settings(uid)
        now = at or health.market_time() or (store.last_candle_time(p, "1m") or time.time())
        out = []
        cs = store.get_candles(p, "5m", limit=400, end=int(now) + 1)
        seen = set()
        for day_offset in (-86400, 0, 86400, 2 * 86400, 3 * 86400):      # yesterday to the next trading day after a weekend
            for w in windows_for(p, now + day_offset, st.sessions, cfg.setup_expiry_minutes):
                if (w.session, w.day) in seen:
                    continue
                seen.add((w.session, w.day))
                rng = opening_range(cs, p, w) if now >= w.range_end else None
                d = w.to_dict()
                d["range"] = rng.to_dict() if rng else None
                out.append(d)
        out.sort(key=lambda d: d["range_start"])
        return {"pair": p, "market_time": iso(now), "sessions": out}

    @app.get("/scan")
    def scanner(uid: str = Depends(user)):
        st = _settings(uid)
        now = health.market_time() or time.time()
        cs = {p: store.get_candles(p, "5m", limit=200, end=int(now) + 1) for p in st.pairs}
        ws = {p: windows_for(p, now, st.sessions, cfg.setup_expiry_minutes) for p in st.pairs}
        return {"market_time": iso(now), "results": scan(cs, ws, now),
                "note": "A scan lists charts that match a rule, not trades to take."}

    @app.get("/topdown")
    def topdown_view(pair: str | None = None, uid: str = Depends(user)):
        """Every strategy on every pair: the top-down pullback, the day-trader setups (20-day breakout,
        flag, 20-day average, RSI bounce, live opening-range breakouts), JeaFx-style supply and demand,
        and CRT on H4/M15 and D1/H1. Each plan is checked by the risk check and the Time Machine,
        graded, and ranked best first."""
        from ..strategy import playbook, topdown
        st = _settings(uid)
        now = int(health.market_time() or time.time())
        pairs = [_pair(pair)] if pair else list(st.pairs)
        rates = {}
        for p in st.pairs:
            last = store.get_candles(p, "1m", limit=1)
            if last:
                rates[p] = last[-1].close
        plans, views = [], []
        for p in pairs:
            ins = instruments.get(p)
            m1 = store.get_candles(p, "1m", None, now + 1, now - (cfg.backfill_days + 5) * 86400)
            if len(m1) < 600:
                views.append({"pair": p, "status": "no data",
                              "summary": "Not enough history yet: %d one-minute candles." % len(m1)})
                continue
            tfs = topdown.frames(m1, now)
            td = topdown.analyse(p, m1, ins, now, tfs)
            views.append({"pair": p, "timeframes": td["timeframes"], "side": td.get("side"),
                          "status": td.get("status"), "summary": td.get("summary")})
            found = [td] if td.get("plan") else []
            found += playbook.daily_setups(p, ins, tfs["1d"], tfs["1h"])
            found += playbook.supply_demand(p, ins, tfs["4h"], tfs["15m"])
            found += playbook.crt(p, ins, tfs["4h"], tfs["15m"], "H4", "M15")
            found += playbook.crt(p, ins, tfs["1d"], tfs["1h"], "D1", "H1")
            five = store.get_candles(p, "5m", None, now + 1, now - cfg.tm_lookback_days * 86400)
            h1 = tfs["1h"][-200:]
            for item in found:
                item.setdefault("timeframes", td["timeframes"])
                item.setdefault("price", tfs["5m"][-1].close if tfs["5m"] else None)
                plans.append(topdown.evaluate(item, five, h1, cfg, st, rates, calendar.events, now))
            # the opening-range breakouts the signal engine is tracking right now, already checked and graded
            for status in ("triggered", "waiting"):
                for sig in store.get_signals(status=status, pair=p, shown=True, limit=10):
                    if sig.get("sl") is None:
                        continue
                    plans.append({"pair": p, "strategy": "Opening-range breakout (%s)" % sig.get("setup", ""),
                                  "side": sig.get("side"), "entry_tf": "M5", "timeframes": td["timeframes"],
                                  "status": "enter now" if status == "triggered" else "wait",
                                  "trigger": "a 5-minute candle closed beyond the range" if status == "triggered"
                                  else "waiting for a 5-minute close beyond the range",
                                  "summary": "Day Trader opening-range breakout, checked by the signal engine.",
                                  "plan": {"entry": sig["entry"], "stop": sig["sl"], "target": sig["tp1"],
                                           "risk_pips": sig.get("sl_pips"), "reward_risk": 1.0},
                                  "timemachine": sig.get("timemachine"), "risk_check": sig.get("risk_check"),
                                  "expectancy_r": sig.get("expectancy_r"), "grade": sig.get("grade"),
                                  "passed": bool(sig.get("passed")), "reasons": sig.get("reasons") or []})
        order = {"A+": 0, "A": 1, "B": 2}

        def rank(x):
            g = (x.get("grade") or {}).get("grade")
            tmx = x.get("timemachine") or {}
            edge = (tmx.get("odds") or 0) - (tmx.get("break_even") or 1)
            return (order.get(g, 3), 0 if x.get("status") == "enter now" else 1, -edge)

        plans.sort(key=rank)
        best = next((x for x in plans if x.get("passed") and x.get("status") == "enter now"), None) \
            or next((x for x in plans if x.get("passed")), None)
        return {"at": iso(now), "best": best, "results": plans, "pairs": views,
                "note": "Rules, not predictions: each strategy is written as fixed rules and every plan is checked "
                        "the same way. A grade says how strong the evidence is, not that a trade will win. "
                        "Nothing is ever placed."}

    @app.get("/calendar")
    async def cal(hours: int = 48, uid: str = Depends(user)):
        await calendar.refresh()
        now = health.market_time() or time.time()
        evs = [e.to_dict() for e in calendar.events if now - 3600 <= e.time <= now + hours * 3600]
        return {"events": evs, "error": calendar.error, "source": cfg.calendar_url or cfg.calendar_file}

    # ------------------------------------------------------------------ signals
    @app.get("/signals")
    def signals(status: str = "active", pair: str | None = None, include_filtered: bool = False,
                limit: int = Query(100, le=1000), uid: str = Depends(user)):
        rows = store.get_signals(status=None if status == "all" else status, pair=pair.upper() if pair else None,
                                 shown=None if include_filtered else True, limit=limit)
        return {"signals": rows, "model_up": health.up("model"),
                "unavailable": not health.up("model")}

    @app.get("/signals/history")
    def history(pair: str | None = None, include_filtered: bool = False, limit: int = Query(200, le=2000),
                uid: str = Depends(user)):
        rows = store.get_signals(status="closed", pair=pair.upper() if pair else None,
                                 shown=None if include_filtered else True, limit=limit)
        return {"signals": rows, "stats": stats_of(rows)}

    @app.get("/signals/{sid}/tradingview")
    def signal_tradingview(sid: str, hold_minutes: int = 120, uid: str = Depends(user)):
        from ..tradingview import pine_for_signal, tradingview_url
        s = store.get_signal(sid)
        if not s:
            raise HTTPException(404, "no such signal")
        try:
            script = pine_for_signal(s, hold_minutes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"script": script, "tradingview_url": tradingview_url(s["pair"]),
                "how": "In TradingView open Pine Editor, paste the script and press Add to chart."}

    @app.get("/signals/{sid}")
    def signal(sid: str, uid: str = Depends(user)):
        s = store.get_signal(sid)
        if not s:
            raise HTTPException(404, "no such signal")
        return s

    @app.post("/signals/{sid}/action")
    async def action(sid: str, body: ActionBody, uid: str = Depends(user)):
        if body.action not in ("took", "skipped"):
            raise HTTPException(400, "action must be took or skipped")
        s = store.get_signal(sid)
        if not s:
            raise HTTPException(404, "no such signal")
        s["user_action"] = body.action
        s["user_action_at"] = iso(time.time())
        s.setdefault("events", []).append({"time": s["user_action_at"], "status": "user_" + body.action})
        store.save_signal(s)
        await bus.publish("signals", {"type": "signal", "signal": s})
        return s

    @app.get("/stats")
    def stats(days: int = 90, include_filtered: bool = False, uid: str = Depends(user)):
        rows = store.get_signals(status="closed", shown=None if include_filtered else True,
                                 since=int(time.time()) - days * 86400 if days else None, limit=5000)
        return stats_of(rows)

    @app.get("/monitor")
    def monitor(days: int = 7, uid: str = Depends(user)):
        from ..monitor import weekly_report
        return weekly_report(store, cfg, days=days, now=health.market_time())

    # ------------------------------------------------------------------ settings and health
    @app.get("/settings")
    def get_settings(uid: str = Depends(user)):
        return _settings(uid).to_dict()

    @app.put("/settings")
    async def put_settings(body: dict, uid: str = Depends(user)):
        cur = _settings(uid).to_dict()
        cur.update(body)
        try:
            st = UserSettings.from_dict(cur)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        for p in st.pairs:
            _pair(p)
        store.put_settings(st.to_dict(), uid)
        await bus.publish("status", {"type": "status", "service": "settings", "ok": True, "ts": time.time()})
        return st.to_dict()

    @app.get("/auth")
    def auth_info():
        return {"login_required": cfg.auth_mode != "none", "mode": cfg.auth_mode}

    @app.get("/auth/check")
    def auth_check(uid: str = Depends(user)):
        return {"ok": True}

    @app.get("/health")
    def health_ep():
        now = health.market_time()
        delayed = {}
        for p in cfg.pairs:
            last = store.last_candle_time(p, "1m")
            if now and last is not None:
                delayed[p] = (now - (last + 60)) > 180
        return {"ok": health.up("feed"), "feed_up": health.up("feed"), "model_up": health.up("model"),
                "signals_available": health.up("model"), "services": health.services,
                "market_time": iso(now) if now else None, "data_delayed": delayed,
                "config": {k: v for k, v in cfg.public().items() if k in (
                    "provider", "pairs", "model_backend", "kronos_model", "horizon", "paths", "sessions",
                    "min_confidence", "auth_mode", "explain", "require_htf_agreement", "volatility_filter",
                    "regime_filter", "tm_synthetic")}}

    # ------------------------------------------------------------------ live stream
    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        try:
            ws_user(cfg, ws, getattr(user, "lockout", None))
        except PermissionError as e:
            await ws.close(code=4401, reason=str(e))
            return
        await ws.accept()
        sub = await bus.subscribe(["candles", "ticks", "forecasts", "signals", "alerts", "status"])
        want = {"pairs": None}

        async def reader():
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "subscribe":
                    ps = msg.get("pairs")
                    want["pairs"] = set(p.upper() for p in ps) if ps else None

        rtask = asyncio.create_task(reader())
        try:
            await ws.send_json({"type": "hello", "server_time": time.time(), "market_time": health.market_time(),
                                "model_up": health.up("model")})
            while True:
                getter = asyncio.create_task(sub.get())
                done, _ = await asyncio.wait({getter, rtask}, return_when=asyncio.FIRST_COMPLETED)
                if rtask in done:
                    getter.cancel()
                    break
                ch, msg = getter.result()
                pair = msg.get("pair") or (msg.get("signal") or {}).get("pair") or (msg.get("forecast") or {}).get("pair")
                if want["pairs"] and pair and ch in ("candles", "ticks", "forecasts") and pair not in want["pairs"]:
                    continue
                if ch == "forecasts":
                    if not msg.get("ok"):
                        continue
                    msg = {"type": "forecast", "reason": msg.get("reason"), "forecast": _slim_forecast(msg["forecast"])}
                elif ch == "status":
                    msg = {"type": "status", "service": msg.get("service"), "ok": msg.get("ok"),
                           "market_time": msg.get("market_time"), "error": msg.get("error"),
                           "backend": msg.get("backend")}
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rtask.cancel()
            await sub.close()

    # ------------------------------------------------------------------ the web app
    app_dir = Path(APP_DIR) / "app"
    if app_dir.exists():
        app.mount("/static", StaticFiles(directory=str(app_dir)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(app_dir / "index.html"))

        @app.get("/sw.js")
        def sw():
            return FileResponse(str(app_dir / "sw.js"), media_type="application/javascript")

        @app.get("/manifest.webmanifest")
        def manifest():
            return FileResponse(str(app_dir / "manifest.webmanifest"), media_type="application/manifest+json")

    if engine:
        # on-demand Kronos and Day Trader analysis for any pair and timeframe (the Mac app's Analyse button)
        from ..engine_server import create_engine_app
        app.mount("/engine", create_engine_app(cfg, forecaster=forecaster,
                                               token=cfg.api_token if cfg.auth_mode == "token" else ""))

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


def stats_of(rows: list) -> dict:
    """Per setup and pair: trades with an outcome, win rate, expectancy in R."""
    groups: dict = {}
    for s in rows:
        if s.get("outcome_r") is None:
            continue
        for key in ("all", "%s %s" % (s["pair"], s.get("setup")), "setup:%s" % s.get("setup")):
            g = groups.setdefault(key, {"trades": 0, "wins": 0, "sum_r": 0.0, "took": 0})
            g["trades"] += 1
            g["wins"] += s["outcome_r"] > 0
            g["sum_r"] += s["outcome_r"]
            g["took"] += s.get("user_action") == "took"
    out = {}
    for k, g in groups.items():
        out[k] = {"trades": g["trades"], "win_rate": round(g["wins"] / g["trades"], 3),
                  "expectancy_r": round(g["sum_r"] / g["trades"], 3), "total_r": round(g["sum_r"], 2),
                  "took": g["took"]}
    return out
