"""Top-down analysis: the higher timeframes decide the direction, the lower ones the entry.

1. Trend on D1, H4 and H1: price against its 20 and 50 EMAs, the 20 EMA's slope, and the swing
   structure (higher highs and lows, or lower ones). Each scores -3..+3.
2. Direction only when H4 and H1 agree (D1 adds strength, and is noted when it disagrees).
3. Entry on M15: a pullback to the 15-minute 20 EMA in the trend's direction; the stop beyond the
   last 15-minute swing (at least one 15-minute ATR); the target at the next higher-timeframe level
   (previous day's high or low, or the last H4 swing) when that is at least 1.5R away, else 2R.
4. Trigger on M5: "enter now" when price is in the entry zone and the last 5-minute candle closed in
   the trend's direction; "wait for the pullback" when price is still away from it; "invalid" when
   price has gone through the stop.

These are rules, not predictions. Each plan is then checked like every other setup: the Time
Machine replays similar past situations and the Day Trader risk check sizes it.
"""
from __future__ import annotations

from ..data.candles import resample

TF_ORDER = ("1d", "4h", "1h", "15m", "5m")


def ema(values: list, n: int) -> list:
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def atr(candles: list, n: int = 14) -> float | None:
    if len(candles) < n + 1:
        return None
    trs = []
    for prev, c in zip(candles[-n - 1:-1], candles[-n:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return sum(trs) / len(trs)


def swings(candles: list, width: int = 2) -> tuple[list, list]:
    """Swing highs and lows: a candle whose high (low) is the extreme of ``width`` candles each side."""
    highs, lows = [], []
    for i in range(width, len(candles) - width):
        h, l = candles[i].high, candles[i].low
        win = candles[i - width:i + width + 1]
        if h == max(c.high for c in win):
            highs.append((candles[i].open_time, h))
        if l == min(c.low for c in win):
            lows.append((candles[i].open_time, l))
    return highs, lows


def trend(candles: list, fast: int = 20, slow: int = 50, min_n: int = 30) -> dict:
    """Score -3..+3 and a label for one timeframe."""
    if len(candles) < min_n:
        return {"bias": "unknown", "score": 0, "why": "fewer than %d candles" % min_n}
    closes = [c.close for c in candles]
    e20, e50 = ema(closes, fast), ema(closes, slow)
    last = closes[-1]
    score, why = 0, []
    if last > e50[-1]:
        score += 1
        why.append("above the %d EMA" % slow)
    elif last < e50[-1]:
        score -= 1
        why.append("below the %d EMA" % slow)
    slope = e20[-1] - e20[-6]
    if slope > 0 and e20[-1] > e50[-1]:
        score += 1
        why.append("%d EMA rising above the %d" % (fast, slow))
    elif slope < 0 and e20[-1] < e50[-1]:
        score -= 1
        why.append("%d EMA falling below the %d" % (fast, slow))
    highs, lows = swings(candles)
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
        lh, ll = highs[-1][1] < highs[-2][1], lows[-1][1] < lows[-2][1]
        if hh and hl:
            score += 1
            why.append("higher highs and higher lows")
        elif lh and ll:
            score -= 1
            why.append("lower highs and lower lows")
        else:
            why.append("mixed swings")
    bias = "up" if score >= 2 else ("down" if score <= -2 else "neutral")
    return {"bias": bias, "score": score, "why": ", ".join(why),
            "ema20": round(e20[-1], 6), "ema50": round(e50[-1], 6)}


def frames(candles_1m: list, now: int) -> dict:
    """D1, H4, H1, M15 and M5 candles that are over, built from 1-minute candles."""
    from ..config import TIMEFRAMES
    tfs = {tf: resample(candles_1m, tf) for tf in TF_ORDER}
    return {tf: [c for c in cs if c.open_time + TIMEFRAMES[tf] <= now] for tf, cs in tfs.items()}


def analyse(pair: str, candles_1m: list, ins, now: int, tfs: dict | None = None) -> dict:
    """The top-down view of one pair from its 1-minute candles (30 days or more is best)."""
    tfs = tfs or frames(candles_1m, now)
    # the daily chart has only a few weeks of candles: shorter averages there (5 and 10 days)
    view = {tf: (trend(tfs[tf], 5, 10, 12) if tf == "1d" else trend(tfs[tf])) for tf in TF_ORDER}
    out = {"pair": pair, "timeframes": view, "at": now, "strategy": "Top-down pullback", "entry_tf": "M15"}
    h4, h1, d1 = view["4h"]["score"], view["1h"]["score"], view["1d"]["score"]
    if view["4h"]["bias"] == "up" and view["1h"]["bias"] == "up":
        side = "buy"
    elif view["4h"]["bias"] == "down" and view["1h"]["bias"] == "down":
        side = "sell"
    else:
        out.update(side=None, status="no trade",
                   summary="The 4-hour and 1-hour trends don't agree (%s vs %s): no direction to trade."
                   % (view["4h"]["bias"], view["1h"]["bias"]))
        return out
    sgn = 1 if side == "buy" else -1
    alignment = (h4 + h1 + d1) * sgn                  # 0..9: how strongly the timeframes agree
    m15, m5, d1c, h4c = tfs["15m"], tfs["5m"], tfs["1d"], tfs["4h"]
    a15 = atr(m15)
    if len(m15) < 60 or not a15 or not m5:
        out.update(side=side, status="no trade", summary="Not enough 15-minute history yet.")
        return out
    e15 = ema([c.close for c in m15], 20)[-1]
    price = m5[-1].close
    entry = ins.round_price(e15)
    # stop beyond the last 15-minute swing against the trade, at least one ATR from the entry
    highs15, lows15 = swings(m15[-60:])
    if side == "buy":
        swing = min((l for _, l in lows15[-2:]), default=entry - a15)
        stop = min(swing, entry - a15) - 0.2 * a15
    else:
        swing = max((h for _, h in highs15[-2:]), default=entry + a15)
        stop = max(swing, entry + a15) + 0.2 * a15
    min_risk = 4 * ins.typical_spread_pips * ins.pip             # never tighter than four spreads
    if abs(entry - stop) < min_risk:
        stop = entry - sgn * min_risk
    stop = ins.round_price(stop)
    risk = abs(entry - stop)
    # target: the next higher-timeframe level in the trade's direction, if at least 1.5R away
    levels = []
    if len(d1c) >= 2:
        levels += [d1c[-2].high, d1c[-2].low]
    h4h, h4l = swings(h4c[-60:])
    levels += [v for _, v in h4h[-3:]] + [v for _, v in h4l[-3:]]
    ahead = sorted((lv for lv in levels if sgn * (lv - entry) >= 1.5 * risk), key=lambda lv: sgn * (lv - entry))
    target = ins.round_price(ahead[0]) if ahead else ins.round_price(entry + sgn * 2 * risk)
    target_note = "next higher-timeframe level" if ahead else "2R (no higher-timeframe level within reach)"
    # M5 trigger
    last5 = m5[-1]
    in_zone = abs(price - entry) <= 0.35 * a15
    closed_with = (last5.close > last5.open) if side == "buy" else (last5.close < last5.open)
    if sgn * (price - stop) <= 0:
        status, note = "invalid", "price has gone through the stop: wait for a new setup"
    elif in_zone and closed_with:
        status, note = "enter now", "price is at the 15-minute 20 EMA and the last 5-minute candle closed %s" % (
            "up" if side == "buy" else "down")
    elif in_zone:
        status, note = "almost", "price is at the entry zone: wait for a 5-minute candle to close %s" % (
            "up" if side == "buy" else "down")
    elif sgn * (price - entry) > 0:
        status, note = "wait for the pullback", "price is %.1f pips from the entry zone" % (abs(price - entry) / ins.pip)
    else:
        status, note = "wait", "price is through the entry zone but not the stop"
    out.update(side=side, status=status, alignment=alignment, price=price,
               plan={"entry": entry, "stop": stop, "target": target, "target_note": target_note,
                     "risk_pips": round(risk / ins.pip, 1), "reward_risk": round(abs(target - entry) / risk, 2),
                     "stop_note": "beyond the last 15-minute swing, at least one 15-minute ATR"},
               trigger=note,
               summary="%s: 4H and 1H trend %s%s. Plan: %s a pullback to %s, stop %s, target %s (%s)."
               % ("Strong" if alignment >= 7 else "Aligned", "up" if side == "buy" else "down",
                  "" if view["1d"]["bias"] in (view["4h"]["bias"], "neutral", "unknown") else " (the daily trend disagrees)",
                  side, entry, stop, target, target_note))
    return out


def evaluate(td: dict, five: list, h1: list, cfg, settings, rates: dict, events: list, now: int) -> dict:
    """Check a top-down plan like any setup: Day Trader risk check (size, limits, news), the Time
    Machine (replays of similar past situations), the market regime; then grade it."""
    from zoneinfo import ZoneInfo

    from .. import instruments
    from ..signals.grade import grade_signal
    from . import regime as regime_mod
    from . import timemachine as tm
    from .risk import check_order
    from .sizing import size_position

    if not td.get("plan") or td.get("status") == "invalid":
        td["grade"] = None
        td["passed"] = False
        return td
    ins = instruments.get(td["pair"])
    p, side = td["plan"], td["side"]
    spread = ins.typical_spread_pips * ins.pip
    fill = p["entry"] + spread if side == "buy" else p["entry"]
    reasons = []
    tz = ZoneInfo("Europe/London")
    try:
        pool = tm.build_pool(five, tz, now, cfg.tm_lookback_days)
        mc = tm.simulate(pool, side, p["entry"], fill, p["stop"], p["target"], now, 48, spread, tz,
                         cfg.tm_replays, cfg.tm_seed)
    except ValueError as e:
        mc = {"error": str(e)}
        reasons.append("Time Machine: %s" % e)
    if mc.get("odds") is not None and mc["odds"] < mc["break_even"]:
        reasons.append("Time Machine: %s" % mc.get("verdict"))
    cost_r = 2 * cfg.slippage_pips * ins.pip / max(abs(fill - p["stop"]), ins.pip)
    tm_exp = None if mc.get("avg_R") is None else round(mc["avg_R"] - cost_r, 3)
    if tm_exp is not None and tm_exp <= cfg.min_expectancy_r:
        reasons.append("Time Machine expectancy %s R after costs" % tm_exp)
    try:
        sz = size_position(td["pair"], fill, p["stop"], settings, rates, cfg.commission_per_lot)
        sizing = sz.to_dict()
        order = {"id": "topdown:" + td["pair"], "pair": td["pair"], "side": side, "entry": fill, "sl": p["stop"],
                 "tp1": p["target"], "lots": sz.lots, "max_loss": sz.max_loss, "setup_key": td["pair"] + ":topdown"}
        spreads = [c.spread for c in five[-288:] if c.spread and c.spread > 0]
        spread_now = five[-1].spread if five and five[-1].spread > 0 else None
        spread_normal = sorted(spreads)[len(spreads) // 2] if spreads else None
        rr = check_order(order, settings, now, events=events, spread_now=spread_now, spread_normal=spread_normal)
        risk = rr.to_dict()
        if not rr.passed:
            reasons += ["Day Trader risk check: " + r for r in rr.reasons]
    except ValueError as e:
        sizing, risk = {"error": str(e)}, {"verdict": "BLOCK", "reasons": [str(e)], "warnings": []}
        reasons.append("risk check: cannot size it (%s)" % e)
    reg = regime_mod.detect(h1) if h1 else None
    td.update(timemachine=mc, expectancy_r=tm_exp, sizing=sizing, risk=risk, risk_check=risk.get("verdict"),
              reasons=reasons, passed=not reasons, extras={"regime": reg})
    td["grade"] = grade_signal(td, False)
    return td
