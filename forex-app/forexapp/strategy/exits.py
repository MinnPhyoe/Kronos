"""Stop loss and take profit (trader-exit, ported to forex and given the Kronos check).

Stop loss
1. Structural stop: the range midpoint (``or_mid``, tighter) or the opposite
   side of the range (``or_far``, wider), plus a buffer of one spread.
2. Minimum distance: 0.5 x ATR(14) on 5m, so normal noise does not stop you out.
3. Kronos check: the 10th percentile of the forecast paths' lowest lows (for
   a buy) over their first ``stop_check_bars`` candles (30 minutes on 5m by
   default). If the stop is above it, the stop sits where many paths dip:
   widen it to just beyond that level, or skip the trade if that makes the
   risk too big (more than ``max_risk_atr`` x ATR). Over the full 2-hour
   horizon that percentile sits 5-10 ATR away for any model, so the window is
   short and configurable; tune it in the Phase 5 backtest.
The stop is rounded away from the entry, to the quoted precision.

Take profit
* TP1 = 1R (profit equal to the amount risked).
* TP2 = the level the median forecast path reaches within the horizon, capped
  at 3R: the median, across paths, of each path's highest high (buy) - the
  level half of the paths reach. If that is short of 1R, there is no signal.
Without a forecast (the rules-only backtest), TP2 is 2R.

Prices: candles are bid. A buy fills at the ask (bid + spread) and exits at
the bid; a sell fills at the bid and exits at the ask. Short-side checks
against bid candles add the spread.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..instruments import Instrument


class PlanRejected(Exception):
    """Why there is no plan. ``kind`` groups the reasons for reports."""

    def __init__(self, message: str, kind: str = "plan"):
        super().__init__(message)
        self.kind = kind


def forecast_levels(forecast, side: str, spread: float, check_bars: int = 6, pct: float = 10.0) -> tuple:
    """(path_extreme, median_reach) for the plan, in the prices the exits are checked on.

    path_extreme: buy, the ``pct`` percentile of each path's lowest low over its first
        ``check_bars`` candles (bid); sell, the (100 - pct) percentile of the highest
        highs plus the spread (ask).
    median_reach: buy, the median of each path's highest high over the horizon (bid);
        sell, the median of the lowest lows plus the spread (ask).
    """
    import numpy as np
    p = forecast.paths
    n = max(1, min(check_bars, p.shape[1]))
    if side == "buy":
        return (float(np.percentile(p[:, :n, 2].min(axis=1), pct)), float(np.median(p[:, :, 1].max(axis=1))))
    return (float(np.percentile(p[:, :n, 1].max(axis=1), 100 - pct)) + spread,
            float(np.median(p[:, :, 2].min(axis=1))) + spread)


@dataclass
class ExitPlan:
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk: float
    stop_rule: str
    widened: bool = False
    notes: list = field(default_factory=list)

    def pips(self, ins: Instrument) -> dict:
        return {"sl_pips": round(ins.pips(self.risk), 1), "tp1_pips": round(ins.pips(abs(self.tp1 - self.entry)), 1),
                "tp2_pips": round(ins.pips(abs(self.tp2 - self.entry)), 1)}

    def r_of(self, price: float) -> float:
        return ((price - self.entry) if self.side == "buy" else (self.entry - price)) / self.risk


def plan_exit(ins: Instrument, side: str, entry: float, range_high: float, range_low: float, atr: float | None,
              spread: float, *, stop_rule: str = "or_mid", min_stop_atr: float = 0.5,
              stop_buffer_spreads: float = 1.0, max_risk_atr: float = 3.0, path_extreme: float | None = None,
              median_reach: float | None = None, tp2_cap_r: float = 3.0, atr_multiplier: float = 1.0) -> ExitPlan:
    """Build the plan or raise PlanRejected with the reason.

    path_extreme / median_reach: from ``forecast_levels``.
    """
    if side not in ("buy", "sell"):
        raise PlanRejected("side must be buy or sell")
    long = side == "buy"
    sgn = 1 if long else -1
    notes = []
    buffer = stop_buffer_spreads * spread
    if stop_rule == "or_mid":
        structural = (range_high + range_low) / 2.0
        rule = "range midpoint"
    elif stop_rule == "or_far":
        structural = range_low if long else range_high
        rule = "opposite side of the range"
    else:
        raise PlanRejected("unknown stop rule %r (or_mid, or_far)" % stop_rule)
    stop = structural - sgn * buffer
    if sgn * (entry - stop) <= 0:
        raise PlanRejected("the structural stop %.5f is not %s the entry %.5f" % (stop, "below" if long else "above", entry))
    if atr:
        min_d = min_stop_atr * atr * atr_multiplier
        if sgn * (entry - stop) < min_d:
            stop = entry - sgn * min_d
            notes.append("stop moved to %.2g x ATR(14) from the entry (minimum distance)" % (min_stop_atr * atr_multiplier))
            rule += " / min ATR"
    widened = False
    if path_extreme is not None and sgn * (stop - path_extreme) > 0:
        new_stop = path_extreme - sgn * max(buffer, ins.pip)
        if atr and sgn * (entry - new_stop) > max_risk_atr * atr:
            raise PlanRejected("the forecast paths dip past the stop; widening it to %s needs %.1f x ATR, over the %.1f limit"
                               % (ins.round_price(new_stop), sgn * (entry - new_stop) / atr, max_risk_atr),
                               "forecast paths dip past the stop (widening too big)")
        stop = new_stop
        widened = True
        notes.append("stop widened beyond the 10th percentile of the forecast paths' extremes")
        rule += " / widened for Kronos"
    stop = ins.round_away(stop, entry)
    risk = sgn * (entry - stop)
    if risk <= 0:
        raise PlanRejected("risk is zero after rounding")
    if risk < 2 * spread:
        raise PlanRejected("risk %.1f pips is under two spreads: too tight to survive the spread" % ins.pips(risk),
                           "stop too tight for the spread")
    tp1 = ins.round_toward(entry + sgn * risk, entry)
    if median_reach is not None:
        if sgn * (median_reach - tp1) < 0:
            raise PlanRejected("the median forecast path does not reach 1R (%s; half the paths reach %s)"
                               % (ins.round_price(tp1), ins.round_price(median_reach)), "median path short of 1R")
        cap = entry + sgn * tp2_cap_r * risk
        tp2 = median_reach if sgn * (median_reach - cap) <= 0 else cap
        tp2 = ins.round_toward(tp2, entry)
        if sgn * (tp2 - tp1) < 0:
            tp2 = tp1
    else:
        tp2 = ins.round_toward(entry + sgn * 2 * risk, entry)
    return ExitPlan(side, ins.round_price(entry), stop, tp1, tp2, risk, rule, widened, notes)
