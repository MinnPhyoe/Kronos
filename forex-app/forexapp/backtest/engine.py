"""Phase 5: backtesting the signals on data the model never saw.

Avoid look-ahead (the most common backtest bug):
* at each candle, only candles that had closed by then are used;
* the forecast's input ends at the signal (trigger) candle, never later;
* entries fill at the NEXT candle's open after the trigger, plus the spread
  (buys) and slippage; stops fill at the stop minus slippage; a candle that
  touches both the stop and the target counts as the stop.

``candidate_trades`` runs the rules over the history once and records, for
every trade, the forecast numbers (confidence, Time Machine odds, expected R)
next to the real outcome. Versions and thresholds are then applied on top:
1. rules alone (no Kronos), 2. rules + base Kronos, 3. rules + fine-tuned Kronos.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .. import instruments
from ..config import Config
from ..signals.engine import path_outcomes
from ..strategy import timemachine as tm
from ..strategy.exits import PlanRejected, forecast_levels, plan_exit
from ..strategy.indicators import atr as atr_of
from ..strategy.scanner import opening_range
from ..strategy.sessions import SESSIONS, window
from ..timeutil import iso, utc_dt

log = logging.getLogger(__name__)


@dataclass
class Trade:
    pair: str
    session: str
    day: str
    side: str
    trigger_time: int
    fill_time: int
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_pips: float
    outcome: str = ""
    exit_price: float = 0.0
    R: float = 0.0
    confidence: float | None = None
    tm_odds: float | None = None
    tm_break_even: float | None = None
    expectancy_est: float | None = None
    model: str | None = None
    notes: list = field(default_factory=list)

    @property
    def month(self) -> str:
        return utc_dt(self.trigger_time).strftime("%Y-%m")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trigger_time_iso"] = iso(self.trigger_time)
        return d


def simulate_outcome(ins, side: str, entry: float, stop: float, target: float, bars: list, risk: float,
                     slippage: float, default_spread: float) -> tuple:
    """Walk the candles from the fill candle (inclusive): stop first, then target, else
    exit at the last candle's close. Returns (outcome, exit_price, R)."""
    long = side == "buy"
    for c in bars:
        spr = c.spread if c.spread > 0 else default_spread
        if long:
            if c.low <= stop:
                px = stop - slippage
                return "sl", px, (px - entry) / risk
            if c.high >= target:
                return "tp1", target, (target - entry) / risk
        else:
            if c.high + spr >= stop:
                px = stop + slippage
                return "sl", px, (entry - px) / risk
            if c.low + spr <= target:
                return "tp1", target, (entry - target) / risk
    last = bars[-1]
    spr = last.spread if last.spread > 0 else default_spread
    px = (last.close - slippage) if long else (last.close + spr + slippage)
    return "time", px, ((px - entry) if long else (entry - px)) / risk


def candidate_trades(candles: list, pair: str, cfg: Config, sessions: list, forecaster=None, cache=None,
                     use_tm: bool = True, progress=None) -> tuple:
    """All rule-based trades over ``candles`` (closed 5m candles, time order).
    Returns (trades, rejected_counts)."""
    ins = instruments.get(pair)
    idx = {c.open_time: i for i, c in enumerate(candles)}
    slip = cfg.slippage_pips * ins.pip
    default_spread = ins.typical_spread_pips * ins.pip
    trades, rejected = [], {}
    if not candles:
        return trades, rejected
    d0 = utc_dt(candles[0].open_time).date() + timedelta(days=1)
    d1 = utc_dt(candles[-1].open_time).date()
    keys = [k for k in sessions if k in SESSIONS and SESSIONS[k].applies(pair)]
    day = d0
    n_days = (d1 - d0).days + 1
    i_day = 0
    while day <= d1:
        i_day += 1
        if progress and i_day % 20 == 0:
            progress(pair, i_day, n_days)
        for key in keys:
            w = window(SESSIONS[key], day, cfg.setup_expiry_minutes)
            if w is None or w.range_start not in idx:
                continue
            i0 = idx[w.range_start]
            rng = opening_range(candles[i0:i0 + 3], pair, w)
            if rng is None:
                rejected["range unusable"] = rejected.get("range unusable", 0) + 1
                continue
            # trigger: the first close beyond the range, closed by the expiry
            trig_i, side = None, None
            j = i0 + 3
            while j < len(candles) and candles[j].close_time <= w.expiry:
                c = candles[j]
                if c.close > rng.high:
                    trig_i, side = j, "buy"
                    break
                if c.close < rng.low:
                    trig_i, side = j, "sell"
                    break
                j += 1
            if trig_i is None:
                continue
            trig = candles[trig_i]
            if trig_i + 1 >= len(candles) or candles[trig_i + 1].open_time != trig.close_time:
                rejected["no next candle to fill"] = rejected.get("no next candle to fill", 0) + 1
                continue
            fill_c = candles[trig_i + 1]
            spread = trig.spread if trig.spread > 0 else default_spread
            entry = (fill_c.open + spread + slip) if side == "buy" else (fill_c.open - slip)
            entry = ins.round_price(entry)
            atr = atr_of(candles[max(0, trig_i - 15):trig_i + 1], 14)
            f = None
            if forecaster is not None:
                f = _forecast(forecaster, cache, candles[max(0, trig_i + 1 - cfg.lookback):trig_i + 1], pair, cfg)
            path_extreme = median_reach = None
            if f is not None:
                path_extreme, median_reach = forecast_levels(f, side, spread, cfg.stop_check_bars, cfg.stop_check_pct)
            try:
                plan = plan_exit(ins, side, entry, rng.high, rng.low, atr, spread, stop_rule=cfg.stop_rule,
                                 min_stop_atr=cfg.min_stop_atr, stop_buffer_spreads=cfg.stop_buffer_spreads,
                                 max_risk_atr=cfg.max_risk_atr, path_extreme=path_extreme,
                                 median_reach=median_reach, tp2_cap_r=cfg.tp2_cap_r)
            except PlanRejected as e:
                rejected[e.kind] = rejected.get(e.kind, 0) + 1
                continue
            end = trig_i + 1 + cfg.horizon
            bars = []
            for c in candles[trig_i + 1:end]:
                if bars and c.open_time - bars[-1].open_time != 300:
                    break                       # a gap (the weekend): exit at the last candle before it
                bars.append(c)
            outcome, px, R = simulate_outcome(ins, side, plan.entry, plan.stop, plan.tp1, bars, plan.risk, slip,
                                              default_spread)
            t = Trade(pair, key, w.day, side, trig.close_time, fill_c.open_time, plan.entry, plan.stop, plan.tp1,
                      plan.tp2, round(ins.pips(plan.risk), 1), outcome, round(px, ins.digits + 1), round(R, 4))
            if f is not None:
                po = path_outcomes(f, side, plan.entry, plan.stop, plan.tp1, spread)
                t.confidence = po["confidence"]
                cost_r = 2 * slip / plan.risk
                t.expectancy_est = round(po["avg_R"] - cost_r, 4) if po["avg_R"] is not None else None
                t.model = f.model
            if use_tm:
                try:
                    pool = tm.build_pool(candles[max(0, i0 - 288 * (cfg.tm_lookback_days + 3)):i0],
                                         SESSIONS[key].tz, w.range_start, cfg.tm_lookback_days)
                    mc = tm.simulate(pool, side, fill_c.open, plan.entry, plan.stop, plan.tp1, fill_c.open_time,
                                     cfg.horizon, spread, SESSIONS[key].tz, cfg.tm_replays, cfg.tm_seed)
                    t.tm_odds, t.tm_break_even = mc["odds"], mc["break_even"]
                except ValueError as e:
                    t.notes.append("time machine: %s" % e)
            trades.append(t)
        day += timedelta(days=1)
    return trades, rejected


def _forecast(forecaster, cache, hist: list, pair: str, cfg: Config):
    if len(hist) < 64:
        return None
    key = "%s|%s|5m|%d|%d|%d|%d" % (getattr(forecaster, "name", "?"), pair, hist[-1].open_time, len(hist),
                                    cfg.horizon, cfg.paths)
    if cache is not None:
        hit = cache.cache_get(key)
        if hit is not None:
            from ..models.forecast import Forecast
            return Forecast.from_dict(hit)
    f = forecaster.forecast(hist, pair, "5m", cfg.horizon, cfg.paths)
    if cache is not None:
        cache.cache_put(key, f.to_dict(include_paths=True))
    return f


def passes(t: Trade, threshold: float | None, min_expectancy: float = 0.0, use_tm: bool = True) -> bool:
    """The live filters: confidence, Time Machine pointing the same way, expectancy above zero."""
    if threshold is None:
        return True
    if t.confidence is None or t.confidence < threshold:
        return False
    if use_tm and (t.tm_odds is None or t.tm_break_even is None or t.tm_odds < t.tm_break_even):
        return False
    if t.expectancy_est is None or t.expectancy_est <= min_expectancy:
        return False
    return True
