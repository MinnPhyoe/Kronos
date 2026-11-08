"""The signal engine (Phase 4): setup + forecast paths -> entry, SL, TP, confidence.

The day-trader rules decide where the levels go; the forecast decides whether
the trade is worth showing.

Entry
* Buy: a 5m candle closes above the session range high. Sell: below the low.
* Candles are bid prices. A buy fills at the ask (level + spread); a sell
  fills at the bid (the level) and pays the spread on exit, where its stop
  and targets are checked against the ask.
* A waiting signal shows the planned levels (the range edge); a triggered one
  uses the trigger candle's close.

Confidence
1. For each forecast path, step through its candles and record whether it hits
   TP1 or SL first (or neither). A candle that touches both counts as SL.
2. Kronos confidence = paths hitting TP1 first / all paths (e.g. 20 of 30 = 67%).
3. Time Machine odds = the simulated share of TP1-first among replays that
   hit a side, for the same entry, SL and TP1.
4. Expectancy (R) = the average path outcome in R (win rate x average win -
   loss rate x average loss), less slippage and commission. The spread is
   already in the fill prices.

Filters (all must pass to show a signal): forecast available, the exit plan
holds (incl. the median path reaching 1R), Kronos confidence >= the minimum
(55-60%), Time Machine odds point the same way (at or above break-even),
expectancy above zero, and the risk check is PASS.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .. import instruments
from ..config import Config, UserSettings
from ..models.forecast import Forecast
from ..strategy import timemachine as tm
from ..strategy.exits import PlanRejected, forecast_levels, plan_exit
from ..strategy.indicators import atr as atr_of
from ..strategy.risk import check_order
from ..strategy.scanner import OpeningRange
from ..strategy.sessions import SESSIONS, in_quiet_window
from ..strategy.sizing import size_position
from ..timeutil import iso

DISCLAIMER = ("Analysis and education, not financial advice. Forecasts and simulations are not promises; "
              "most people who trade lose money. You decide every trade; this app never places one.")


@dataclass
class SignalContext:
    cfg: Config
    settings: UserSettings
    rng: OpeningRange
    side: str
    status: str                        # waiting | triggered
    level: float                       # bid price: the range edge (waiting) or the trigger close (triggered)
    now: int                           # market time: the close of the latest candle
    candles_5m: list                   # closed 5m candles up to now (60+ days for the Time Machine)
    spread: float
    spread_normal: float | None = None
    forecast: Forecast | None = None
    forecast_error: str | None = None
    htf_forecast: Forecast | None = None
    regime: dict | None = None
    rates: dict = field(default_factory=dict)
    today_entries: list = field(default_factory=list)
    active: list = field(default_factory=list)
    events: list = field(default_factory=list)
    trigger_time: int | None = None


def path_outcomes(f: Forecast, side: str, entry: float, stop: float, target: float, spread: float,
                  trigger_level: float | None = None) -> dict:
    """Step each path: TP first, SL first or neither. With ``trigger_level`` (a waiting
    signal), a path only enters once one of its candles closes beyond the level."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    out = {"paths": f.n_paths, "entered": 0, "tp_first": 0, "sl_first": 0, "neither": 0, "R": []}
    for path in f.paths:
        entered = trigger_level is None
        result = None
        for o, h, l, c in path:
            if not entered:
                if (c > trigger_level) if long else (c < trigger_level):
                    entered = True
                continue
            if long:
                if l <= stop:
                    result = "sl"
                elif h >= target:
                    result = "tp"
            else:
                if h + spread >= stop:
                    result = "sl"
                elif l + spread <= target:
                    result = "tp"
            if result:
                break
        if not entered:
            continue
        out["entered"] += 1
        if result == "tp":
            out["tp_first"] += 1
            out["R"].append(((target - entry) if long else (entry - target)) / risk)
        elif result == "sl":
            out["sl_first"] += 1
            out["R"].append(-1.0)
        else:
            out["neither"] += 1
            last = path[-1][3]
            out["R"].append(((last - entry) if long else (entry - (last + spread))) / risk)
    n = out["entered"]
    out["confidence"] = round(out["tp_first"] / n, 4) if n else None
    out["avg_R"] = round(float(np.mean(out["R"])), 4) if n else None
    out["win_rate"] = round(out["tp_first"] / n, 4) if n else None
    wins = [r for r in out["R"] if r > 0]
    losses = [-r for r in out["R"] if r < 0]
    out["avg_win"] = round(float(np.mean(wins)), 3) if wins else 0.0
    out["avg_loss"] = round(float(np.mean(losses)), 3) if losses else 0.0
    del out["R"]
    return out


def _filter(filters, name, ok, detail):
    filters.append({"name": name, "ok": bool(ok), "detail": detail})


def build_signal(ctx: SignalContext) -> dict:
    cfg, st, rng = ctx.cfg, ctx.settings, ctx.rng
    ins = instruments.get(rng.pair)
    long = ctx.side == "buy"
    spread = ctx.spread if ctx.spread and ctx.spread > 0 else ins.typical_spread_pips * ins.pip
    entry = ins.round_price(ctx.level + spread) if long else ins.round_price(ctx.level)
    filters: list = []
    f = ctx.forecast
    atr = atr_of([c for c in ctx.candles_5m if c.close_time <= ctx.now], 14)
    min_conf = st.min_confidence if st.min_confidence is not None else cfg.min_confidence

    sig = {"id": rng.key, "pair": rng.pair, "side": ctx.side, "setup": rng.label, "session": rng.session,
           "trading_day": rng.day, "status": ctx.status, "entry": entry, "sl": None, "tp1": None, "tp2": None,
           "kronos_confidence": None, "timemachine_odds": None, "expectancy_r": None, "risk_check": "BLOCK",
           "lots": 0.0, "forecast_band": [], "created_at": iso(ctx.now), "created_ts": ctx.now,
           "updated_at": iso(ctx.now), "range": rng.to_dict(), "expires_at": iso(rng.expiry),
           "trigger": ({"time": iso(ctx.trigger_time), "price": ctx.level} if ctx.status == "triggered" else None),
           "spread_pips": round(ins.pips(spread), 2), "atr_pips": round(ins.pips(atr), 1) if atr else None,
           "model": {"available": f is not None, "name": f.model if f else None, "error": ctx.forecast_error,
                     "base_time": iso(f.base_time) if f else None},
           "digits": ins.digits, "pip": ins.pip, "disclaimer": DISCLAIMER, "explanation": None,
           "outcome": None, "outcome_r": None, "user_action": None}

    _filter(filters, "forecast", f is not None,
            "forecast from %s" % f.model if f else "model unavailable: %s" % (ctx.forecast_error or "no forecast"))

    # Phase 7: volatility forecast
    atr_mult = 1.0
    vol = None
    if f is not None and atr:
        vol_ratio = f.forecast_atr() / atr
        vol = {"forecast_atr_pips": round(ins.pips(f.forecast_atr()), 1), "ratio": round(vol_ratio, 2)}
        if cfg.volatility_filter:
            if vol_ratio >= cfg.vol_skip_ratio:
                _filter(filters, "volatility", False, "forecast volatility %.1fx the recent ATR: skip before a spike" % vol_ratio)
            elif vol_ratio >= cfg.vol_widen_ratio:
                atr_mult = vol_ratio
                vol["widened"] = True

    # exit plan, with the Kronos stop check and TP2
    path_extreme = median_reach = None
    if f is not None:
        path_extreme, median_reach = forecast_levels(f, ctx.side, spread, cfg.stop_check_bars, cfg.stop_check_pct)
        sig["forecast_band"] = [{"time": b["time"], "p10": ins.round_price(b["p10"]), "p50": ins.round_price(b["p50"]),
                                 "p90": ins.round_price(b["p90"])} for b in f.band()]
    plan = None
    try:
        plan = plan_exit(ins, ctx.side, entry, rng.high, rng.low, atr, spread, stop_rule=cfg.stop_rule,
                         min_stop_atr=cfg.min_stop_atr, stop_buffer_spreads=cfg.stop_buffer_spreads,
                         max_risk_atr=cfg.max_risk_atr, path_extreme=path_extreme, median_reach=median_reach,
                         tp2_cap_r=cfg.tp2_cap_r, atr_multiplier=atr_mult)
        _filter(filters, "plan", True, "stop: %s" % plan.stop_rule)
    except PlanRejected as e:
        _filter(filters, "plan", False, str(e))

    if plan is not None:
        sig.update({"sl": plan.stop, "tp1": plan.tp1, "tp2": plan.tp2, **plan.pips(ins),
                    "plan": {"stop_rule": plan.stop_rule, "widened": plan.widened, "notes": plan.notes}})
        # sizing
        rates = dict(ctx.rates)
        rates[rng.pair] = ctx.level
        try:
            sz = size_position(rng.pair, entry, plan.stop, st, rates, cfg.commission_per_lot)
            sig["lots"] = sz.lots
            sig["sizing"] = sz.to_dict()
        except ValueError as e:
            sz = None
            sig["sizing"] = {"error": str(e)}
        # costs in R
        cost_r = 2 * cfg.slippage_pips * ins.pip / plan.risk
        if sz is not None and sz.pip_value_per_lot > 0 and cfg.commission_per_lot:
            cost_r += cfg.commission_per_lot / (ins.pips(plan.risk) * sz.pip_value_per_lot)
        # Kronos paths
        if f is not None:
            trig = None
            if ctx.status == "waiting":
                trig = rng.high if long else rng.low
            po = path_outcomes(f, ctx.side, entry, plan.stop, plan.tp1, spread, trig)
            po["cost_r"] = round(cost_r, 4)
            po["direction_prob"] = round(f.direction_prob(), 4)
            sig["kronos"] = po
            sig["kronos_confidence"] = po["confidence"]
            if po["avg_R"] is not None:
                sig["expectancy_r"] = round(po["avg_R"] - cost_r, 3)
            conf = po["confidence"]
            _filter(filters, "confidence", conf is not None and conf >= min_conf,
                    ("%d of %d paths hit TP1 first (%.0f%%; minimum %.0f%%)"
                     % (po["tp_first"], po["entered"], 100 * conf, 100 * min_conf)) if conf is not None
                    else "no forecast path breaks out of the range")
            _filter(filters, "expectancy", sig["expectancy_r"] is not None and sig["expectancy_r"] > cfg.min_expectancy_r,
                    "expectancy %s R after costs" % sig["expectancy_r"])
        # Time Machine
        s = SESSIONS[rng.session]
        try:
            pool = tm.build_pool(ctx.candles_5m, s.tz, rng.start, cfg.tm_lookback_days)
            if cfg.tm_synthetic and f is not None:
                tm.add_synthetic(pool, f, s.tz)
            mc = tm.simulate(pool, ctx.side, ctx.level, entry, plan.stop, plan.tp1, ctx.now, cfg.horizon, spread,
                             s.tz, cfg.tm_replays, cfg.tm_seed)
            sig["timemachine"] = mc
            sig["timemachine_odds"] = mc["odds"]
            _filter(filters, "timemachine", mc["odds"] is not None and mc["odds"] >= mc["break_even"],
                    mc["verdict"] + " (seed %d, %d replays)" % (mc["seed"], mc["replays"]))
        except ValueError as e:
            sig["timemachine"] = {"error": str(e)}
            _filter(filters, "timemachine", False, "Time Machine: %s" % e)
        # risk check
        order = {"id": sig["id"], "pair": rng.pair, "side": ctx.side, "entry": entry, "sl": plan.stop, "tp1": plan.tp1,
                 "lots": sig["lots"], "max_loss": (sz.max_loss if sz else 0.0),
                 "setup_key": "%s:%s" % (rng.pair, rng.session)}
        rr = check_order(order, st, ctx.now, today_entries=ctx.today_entries, active=ctx.active, events=ctx.events,
                         entry_window=(rng.end, rng.expiry), quiet=in_quiet_window(rng.session, ctx.now),
                         spread_now=ctx.spread, spread_normal=ctx.spread_normal)
        sig["risk"] = rr.to_dict()
        sig["risk_check"] = rr.verdict
        _filter(filters, "risk", rr.passed, "; ".join(rr.reasons) if rr.reasons else "PASS")

    # Phase 7: multi-timeframe and regime
    extras = {"volatility": vol}
    if cfg.require_htf_agreement:
        h = ctx.htf_forecast
        if h is None:
            _filter(filters, "htf", False, "1H forecast unavailable")
        else:
            dp = h.direction_prob()
            ok = dp >= 0.5 if long else dp <= 0.5
            extras["htf"] = {"direction_prob": round(dp, 3)}
            _filter(filters, "htf", ok, "1H forecast: %.0f%% of paths end higher" % (100 * dp))
    if ctx.regime is not None:
        extras["regime"] = ctx.regime
        if cfg.regime_filter:
            _filter(filters, "regime", ctx.regime.get("label") != "ranging", "regime: %s" % ctx.regime.get("label"))
    sig["extras"] = extras

    sig["filters"] = filters
    sig["passed"] = all(x["ok"] for x in filters)
    sig["reasons"] = [x["detail"] for x in filters if not x["ok"]]
    sig["shown"] = sig["passed"]
    if not sig["passed"]:
        sig["status"] = "filtered"
    sig["computed_ms"] = None
    sig["engine_ts"] = time.time()
    return sig
