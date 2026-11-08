"""Strategy service: scanner, entry, exit, Time Machine, risk check and the signal engine.

On each closed 5m candle:
* a session range that just finished becomes a setup (status ``waiting``): the
  model is asked for a forecast and both sides are evaluated; the side that
  passes every filter (if any) is shown with its planned levels.
* a waiting setup whose candle closes beyond the range is ``triggered``: a
  fresh forecast from the trigger candle decides whether the signal is shown
  (``triggered``) or logged only (``filtered``).
* a setup with no trigger after 90 minutes is ``expired``.
On each closed 1m candle, triggered signals are followed to their outcome
(TP1, SL, or the time exit at the end of the horizon) for the results log.
Every signal is logged, shown or filtered. The app never places a trade.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from .. import instruments
from ..config import Config, UserSettings
from ..models.forecast import Forecast
from ..signals.engine import SignalContext, build_signal
from ..timeutil import iso, market_open, parse_iso, trading_day
from . import regime as regime_mod
from .indicators import median
from .scanner import OpeningRange, evaluate_entry, opening_range
from .sessions import windows_for

log = logging.getLogger(__name__)


@dataclass
class Setup:
    rng: OpeningRange
    state: str = "waiting"           # waiting | triggered | expired
    signal: dict | None = None
    trigger: object = None
    pending: dict = field(default_factory=dict)      # request_id -> {"kind": ..., "sent": monotonic}
    forecasts: dict = field(default_factory=dict)    # kind -> Forecast | error str


class StrategyService:
    def __init__(self, cfg: Config, store, bus, alerter, calendar, explainer=None):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.alerter = alerter
        self.calendar = calendar
        self.explainer = explainer
        self.setups: dict[str, Setup] = {}
        self.done: set = set()
        self.requests: dict[str, str] = {}           # request_id -> setup key
        self.spreads: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.market_now: float = 0.0
        self.tracked: dict[str, dict] = {}           # signal id -> signal (outcome pending)

    # ------------------------------------------------------------------ helpers
    def settings(self) -> UserSettings:
        try:
            return UserSettings.from_dict(self.store.get_settings() or {"pairs": self.cfg.pairs,
                                                                         "sessions": self.cfg.sessions})
        except ValueError:
            return UserSettings()

    def _spread_normal(self, pair: str) -> float:
        ins = instruments.get(pair)
        rows = self.store.get_candles(pair, "1m", limit=240)
        m = median([c.spread for c in rows if c.spread > 0])
        return m if m else ins.typical_spread_pips * ins.pip

    def _spread_now(self, pair: str) -> float | None:
        if pair in self.spreads:
            return self.spreads[pair]
        rows = self.store.get_candles(pair, "1m", limit=1)
        return rows[-1].spread if rows and rows[-1].spread > 0 else None

    def _rates(self) -> dict:
        out = dict(self.prices)
        for p in self.cfg.pairs:
            if p not in out:
                rows = self.store.get_candles(p, "1m", limit=1)
                if rows:
                    out[p] = rows[-1].close
        return out

    def _today(self, now: float, st: UserSettings) -> tuple:
        day = trading_day(now)
        recent = self.store.get_signals(since=int(now) - 3 * 86400, limit=500)
        taken = [s for s in recent if s.get("user_action") == "took" and trading_day(s.get("created_ts", 0)) == day]
        today_entries = [{"id": s["id"], "max_loss": (s.get("sizing") or {}).get("max_loss", 0.0)} for s in taken]
        active = [s for s in recent if s.get("status") in ("waiting", "triggered") and s.get("shown")]
        return today_entries, active

    async def _publish(self, sig: dict):
        await asyncio.to_thread(self.store.save_signal, sig)
        await self.bus.publish("signals", {"type": "signal", "signal": sig})

    async def _request(self, setup: Setup, kind: str, tf: str, base_time: int):
        rid = uuid.uuid4().hex[:12]
        setup.pending[rid] = {"kind": kind, "sent": time.monotonic()}
        self.requests[rid] = setup.rng.key
        await self.bus.publish("forecast_requests", {"id": rid, "pair": setup.rng.pair, "tf": tf,
                                                     "base_time": base_time, "reason": "setup"})

    # ------------------------------------------------------------------ setups
    async def on_5m_close(self, pair: str, candle: dict):
        now = int(candle["open_time"]) + 300
        self.market_now = max(self.market_now, now)
        st = self.settings()
        if pair not in st.pairs:
            return
        sessions = [s for s in st.sessions if s in self.cfg.sessions] or st.sessions
        for w in windows_for(pair, now - 1, sessions, self.cfg.setup_expiry_minutes):
            key = "%s:%s:%s" % (pair, w.session, w.day)
            if key in self.done:
                continue
            setup = self.setups.get(key)
            if setup is None:
                if not (w.range_end <= now < w.expiry):
                    continue
                prior = await asyncio.to_thread(self.store.get_signal, key)
                if prior and prior.get("status") not in ("waiting",):
                    self.done.add(key)
                    continue
                cs = await asyncio.to_thread(self.store.get_candles, pair, "5m", 64, now)
                rng = opening_range(cs, pair, w)
                if rng is None:
                    log.info("%s: opening range not usable (missing candles)", key)
                    self.done.add(key)
                    await asyncio.to_thread(self.store.log_health, "strategy", "range_unusable", {"setup": key})
                    continue
                setup = Setup(rng)
                self.setups[key] = setup
                log.info("setup %s: range %s - %s", key, rng.low, rng.high)
                await self._request(setup, "waiting", "5m", int(candle["open_time"]))
                if self.cfg.require_htf_agreement:
                    await self._htf_request(setup, now)
                # the candle that completed the range can't trigger; fall through to the next one
                continue
            if setup.state != "waiting":
                continue
            cs = await asyncio.to_thread(self.store.get_candles, pair, "5m", 64, now)
            es = evaluate_entry(setup.rng, cs, now)
            if es.status == "triggered":
                setup.state = "triggered"
                setup.trigger = es
                setup.forecasts.pop("trigger", None)
                await self._request(setup, "trigger", "5m", es.trigger_open_time)
                if self.cfg.require_htf_agreement:
                    await self._htf_request(setup, now)
            elif es.status == "expired":
                await self._expire(setup)

    async def _htf_request(self, setup: Setup, now: int):
        hc = await asyncio.to_thread(self.store.get_candles, setup.rng.pair, "1h", 1, now - 3600 + 1)
        if hc:
            await self._request(setup, "htf", "1h", hc[-1].open_time)

    async def _expire(self, setup: Setup):
        setup.state = "expired"
        self.done.add(setup.rng.key)
        sig = setup.signal or {"id": setup.rng.key, "pair": setup.rng.pair, "side": None, "setup": setup.rng.label,
                               "session": setup.rng.session, "trading_day": setup.rng.day,
                               "range": setup.rng.to_dict(), "shown": False, "created_ts": setup.rng.end,
                               "created_at": iso(setup.rng.end)}
        sig = dict(sig)
        sig["status"] = "expired"
        sig["updated_at"] = iso(setup.rng.expiry)
        sig.setdefault("events", []).append({"time": iso(setup.rng.expiry), "status": "expired"})
        await self._publish(sig)
        self.setups.pop(setup.rng.key, None)

    # ------------------------------------------------------------------ forecasts -> signals
    async def on_forecast(self, msg: dict):
        rid = msg.get("request_id")
        key = self.requests.pop(rid, None) if rid else None
        if key is None or key not in self.setups:
            return
        setup = self.setups[key]
        req = setup.pending.pop(rid, None)
        if req is None:
            return
        kind = req["kind"]
        if msg.get("ok"):
            setup.forecasts[kind] = Forecast.from_dict(msg["forecast"])
        else:
            setup.forecasts[kind] = msg.get("error") or "forecast failed"
        await self._maybe_build(setup)

    async def _maybe_build(self, setup: Setup, timed_out: bool = False):
        kind = "trigger" if setup.state == "triggered" else "waiting"
        if kind not in setup.forecasts and not timed_out:
            return
        if self.cfg.require_htf_agreement and "htf" not in setup.forecasts and not timed_out:
            return
        f = setup.forecasts.get(kind)
        err = f if isinstance(f, str) else ("model timeout" if f is None else None)
        f = f if isinstance(f, Forecast) else None
        h = setup.forecasts.get("htf")
        h = h if isinstance(h, Forecast) else None
        st = self.settings()
        rng = setup.rng
        if kind == "trigger":
            es = setup.trigger
            now = es.trigger_time
            sides = [(es.side, es.price, "triggered")]
        else:
            now = rng.end
            sides = [("buy", rng.high, "waiting"), ("sell", rng.low, "waiting")]
        start = rng.start - (self.cfg.tm_lookback_days + 3) * 86400
        cs = await asyncio.to_thread(self.store.get_candles, rng.pair, "5m", None, now, start)
        spread_now = await asyncio.to_thread(self._spread_now, rng.pair)
        spread_normal = await asyncio.to_thread(self._spread_normal, rng.pair)
        rates = await asyncio.to_thread(self._rates)
        today_entries, active = await asyncio.to_thread(self._today, now, st)
        reg = None
        if self.cfg.regime_filter:
            h1 = await asyncio.to_thread(self.store.get_candles, rng.pair, "1h", 24 * 21, now)
            reg = regime_mod.detect(h1, h)
        await self.calendar.refresh()
        best = None
        for side, level, status in sides:
            ctx = SignalContext(self.cfg, st, rng, side, status, level, now, cs,
                                spread_now or spread_normal, spread_normal, f, err, h, reg, rates,
                                today_entries, active, self.calendar.events,
                                trigger_time=now if status == "triggered" else None)
            sig = await asyncio.to_thread(build_signal, ctx)
            rank = (sig["passed"], sig["kronos_confidence"] or 0.0)
            if best is None or rank > best[0]:
                best = (rank, sig)
        sig = best[1]
        prev = setup.signal or await asyncio.to_thread(self.store.get_signal, rng.key) or {}
        sig["events"] = list(prev.get("events", [])) + [{"time": iso(now), "status": sig["status"], "side": sig["side"],
                                                         "passed": sig["passed"]}]
        sig["shown"] = sig["passed"] or bool(prev.get("shown") and kind == "waiting")
        if kind == "trigger" and not sig["passed"]:
            sig["shown"] = False
        sig["user_action"] = prev.get("user_action")
        if kind == "trigger" and self.market_now - now > 300:
            sig["late_minutes"] = int((self.market_now - now) // 60)
            sig.setdefault("reasons", []).append("late: the trigger was %d minutes ago; a late entry is a different trade"
                                                 % sig["late_minutes"])
        sig["created_ts"] = prev.get("created_ts", now)
        sig["created_at"] = prev.get("created_at", iso(now))
        setup.signal = sig
        await self._publish(sig)
        if kind == "trigger":
            self.done.add(rng.key)
            self.setups.pop(rng.key, None)
            self.tracked[sig["id"]] = sig
            if sig["passed"]:
                await self.alerter.send("trig:" + sig["id"], "triggered", "%s %s triggered" % (sig["pair"], sig["side"]),
                                        "%s: entry %s, SL %s, TP1 %s. Confidence %s. Your call: nothing was placed."
                                        % (sig["setup"], sig["entry"], sig["sl"], sig["tp1"],
                                           _pct(sig["kronos_confidence"])), {"signal_id": sig["id"]}, "high")
        elif sig["passed"]:
            await self.alerter.send("wait:" + sig["id"], "setup", "%s setup: %s" % (sig["pair"], sig["setup"]),
                                    "Waiting for a 5m close %s %s." % ("above" if sig["side"] == "buy" else "below",
                                                                       rng.high if sig["side"] == "buy" else rng.low),
                                    {"signal_id": sig["id"]})
        if sig["passed"] and self.explainer is not None:
            asyncio.create_task(self._explain(sig["id"]))

    async def _explain(self, sid: str):
        sig = await asyncio.to_thread(self.store.get_signal, sid)
        if not sig:
            return
        text = await self.explainer.explain(sig)
        if not text:
            return
        latest = await asyncio.to_thread(self.store.get_signal, sid) or sig
        latest["explanation"] = text
        await self._publish(latest)

    async def _timeouts(self):
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            for setup in list(self.setups.values()):
                for rid, req in list(setup.pending.items()):
                    if now - req["sent"] > self.cfg.model_timeout_s:
                        setup.pending.pop(rid, None)
                        self.requests.pop(rid, None)
                        setup.forecasts.setdefault(req["kind"], "model timeout")
                        await self._maybe_build(setup, timed_out=not setup.pending)

    # ------------------------------------------------------------------ outcomes (results log)
    async def on_1m_close(self, pair: str, candle: dict):
        self.market_now = max(self.market_now, int(candle["open_time"]) + 60)
        self.prices[pair] = float(candle["close"])
        if candle.get("spread"):
            self.spreads.setdefault(pair, float(candle["spread"]))
        for sid, sig in list(self.tracked.items()):
            if sig["pair"] != pair or sig.get("sl") is None:
                continue
            res = follow_outcome(sig, candle, self.cfg.horizon)
            if res is None:
                continue
            outcome, exit_price, r = res
            latest = await asyncio.to_thread(self.store.get_signal, sid) or sig
            latest.update({"outcome": outcome, "outcome_r": r, "exit_price": exit_price,
                           "closed_at": iso(int(candle["open_time"]) + 60)})
            if latest.get("status") == "triggered":
                latest["status"] = "closed"
            latest.setdefault("events", []).append({"time": latest["closed_at"], "status": "closed", "outcome": outcome})
            self.tracked.pop(sid, None)
            await self._publish(latest)
            if latest.get("shown"):
                await self.alerter.send("out:" + sid, outcome, "%s %s: %s" % (sig["pair"], sig["side"], outcome.upper()),
                                        "%s closed at %s (%+.2f R)." % (sig["setup"], exit_price, r),
                                        {"signal_id": sid})

    def _restore(self):
        """After a restart: keep following triggered signals that have no outcome yet."""
        for s in self.store.get_signals(status="triggered", limit=200) + self.store.get_signals(status="filtered", limit=200):
            if s.get("trigger") and s.get("outcome") is None and s.get("sl") is not None:
                self.tracked[s["id"]] = s
        for s in self.store.get_signals(status="waiting", limit=200):
            r = s.get("range") or {}
            if not r:
                continue
            rng = OpeningRange(s["pair"], s["session"], s["setup"], s["trading_day"], r["start"], r["end"], r["expiry"],
                               r["high"], r["low"], r.get("bars", 3))
            self.setups[rng.key] = Setup(rng, signal=s)

    # ------------------------------------------------------------------ news alerts
    async def _news_alerts(self):
        while True:
            try:
                await self.calendar.refresh()
                now = self.market_now or time.time()
                st = self.settings()
                if st.alerts_enabled and market_open(now):
                    cur = set()
                    for p in st.pairs:
                        cur |= instruments.currencies(p)
                    for e in self.calendar.upcoming(cur, int(now), int(now) + 15 * 60):
                        await self.alerter.send("news:%s:%d" % (e.currency, e.time), "news",
                                                "%s news in %d min" % (e.currency, max(0, (e.time - now) // 60)),
                                                "%s at %s UTC. New trades are blocked around it." % (e.title, iso(e.time)[11:16]),
                                                e.to_dict())
            except Exception:
                log.exception("news alerts")
            await asyncio.sleep(30)

    async def run(self):
        await asyncio.to_thread(self._restore)
        sub = await self.bus.subscribe(["candles", "forecasts", "ticks"])
        tasks = [asyncio.create_task(self._timeouts()), asyncio.create_task(self._news_alerts())]
        try:
            async for ch, msg in sub:
                try:
                    if ch == "candles" and msg.get("closed"):
                        if msg["tf"] == "5m":
                            await self.on_5m_close(msg["pair"], msg["candle"])
                        elif msg["tf"] == "1m":
                            await self.on_1m_close(msg["pair"], msg["candle"])
                    elif ch == "forecasts":
                        await self.on_forecast(msg)
                    elif ch == "ticks":
                        self.spreads[msg["pair"]] = float(msg["ask"]) - float(msg["bid"])
                        self.prices[msg["pair"]] = float(msg["bid"])
                except Exception:
                    log.exception("strategy failed on %s message", ch)
        finally:
            for t in tasks:
                t.cancel()


def _pct(x):
    return "n/a" if x is None else "%.0f%%" % (100 * x)


def follow_outcome(sig: dict, candle: dict, horizon_bars: int):
    """One closed 1m candle after the entry -> (outcome, exit price, R) or None.
    Buys exit on the bid (the candle); sells on the ask (candle + spread).
    A candle that touches both the stop and TP1 counts as the stop."""
    trig = sig.get("trigger") or {}
    if not trig.get("time"):
        return None
    entry_t = parse_iso(trig["time"])
    t = int(candle["open_time"])
    if t < entry_t:
        return None
    long = sig["side"] == "buy"
    entry, sl, tp1 = sig["entry"], sig["sl"], sig["tp1"]
    risk = (entry - sl) if long else (sl - entry)
    spread = float(candle.get("spread") or 0.0)
    cost = (sig.get("kronos") or {}).get("cost_r", 0.0) or 0.0
    hi, lo, cl = float(candle["high"]), float(candle["low"]), float(candle["close"])
    if long:
        if lo <= sl:
            return "sl", sl, round(-1.0 - cost, 3)
        if hi >= tp1:
            return "tp1", tp1, round((tp1 - entry) / risk - cost, 3)
    else:
        if hi + spread >= sl:
            return "sl", sl, round(-1.0 - cost, 3)
        if lo + spread <= tp1:
            return "tp1", tp1, round((entry - tp1) / risk - cost, 3)
    end = entry_t + horizon_bars * 300
    if t + 60 >= end or not market_open(t + 120):
        px = cl if long else cl + spread
        r = ((px - entry) if long else (entry - px)) / risk
        return "time", round(px, 6), round(r - cost, 3)
    return None
