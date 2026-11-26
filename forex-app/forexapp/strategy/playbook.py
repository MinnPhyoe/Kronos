"""The playbook: every strategy, as fixed rules, each giving an entry plan in the same shape.

From the day-trader-skill repo (built for US stocks, long only; here both directions, with thresholds
scaled to each pair's average daily range instead of stock percentages):
  * 20-day breakout     a daily close beyond the highest (lowest) close of the 20 days before it
  * flag                a strong move (over 3 daily ATRs in 10 days), then 5 days inside 1.2 ATRs
  * 20-day average      the close crosses back over the 20-day simple average
  * RSI bounce          RSI 14 under 30 (over 70) on the day before, then a day closing up (down)
  * opening-range breakout: live in the signal engine; its active signals join this list

JeaFx-style supply and demand (an interpretation of the publicly taught approach): the H4 trend from
its swings; the zone is the last opposite candle before the impulse that made the latest swing; the
entry is the first return to the zone, confirmed by an M15 candle closing in the trend's direction.

CRT, Candle Range Theory: candle 2 sweeps candle 1's high or low and closes back inside its range;
the trade aims at the other side (first at the 50%), with the stop beyond the sweep. Run on H4 with
M15 entries and on D1 with H1 entries; "enter now" once the lower timeframe shifts structure.

Every plan then goes through the same checks (risk check, Time Machine) and is graded. These are
rules, not predictions.
"""
from __future__ import annotations

from .topdown import atr, ema, swings, trend


def sma(xs: list, n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(closes: list, n: int = 14) -> list:
    """Wilder's RSI, one value per close from index n on (earlier values are None)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(0.0, d)) / n
        al = (al * (n - 1) + max(0.0, -d)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def plan(pair, ins, strategy, side, entry, stop, target, status, trigger, summary, tf_entry, min_rr=1.5):
    """One plan in the common shape, or None when the stop is on the wrong side or the reward too small."""
    sgn = 1 if side == "buy" else -1
    min_risk = 4 * ins.typical_spread_pips * ins.pip
    if sgn * (entry - stop) < min_risk:
        stop = entry - sgn * min_risk
    risk = abs(entry - stop)
    if sgn * (target - entry) < min_rr * risk:
        target = entry + sgn * 2 * risk
    entry, stop, target = ins.round_price(entry), ins.round_price(stop), ins.round_price(target)
    if sgn * (entry - stop) <= 0 or sgn * (target - entry) <= 0:
        return None
    return {"pair": pair, "strategy": strategy, "side": side, "status": status, "trigger": trigger,
            "summary": summary, "entry_tf": tf_entry,
            "plan": {"entry": entry, "stop": stop, "target": target,
                     "risk_pips": round(risk / ins.pip, 1), "reward_risk": round(abs(target - entry) / risk, 2)}}


def _entry_status(side, price, entry, stop, zone, closed_with):
    """'enter now' inside the zone with a confirming candle; else where price stands."""
    sgn = 1 if side == "buy" else -1
    if sgn * (price - stop) <= 0:
        return "invalid", "price has gone through the stop"
    if abs(price - entry) <= zone:
        return ("enter now", "price is at the entry and the last candle closed %s" % ("up" if side == "buy" else "down")) \
            if closed_with else ("almost", "price is at the entry: wait for a candle to close %s" % (
                "up" if side == "buy" else "down"))
    if sgn * (price - entry) > 0:
        return "wait", "waiting for price to come back to %s" % entry
    return "wait", "price is past the entry but not the stop"


# ------------------------------------------------------------------ the day-trader daily setups
def daily_setups(pair, ins, d1, h1) -> list:
    if len(d1) < 25 or len(h1) < 30:
        return []
    closes = [c.close for c in d1]
    a = atr(d1) or 0
    if a <= 0:
        return []
    price, out = h1[-1].close, []
    last_h1 = h1[-1]

    # 20-day breakout: the last daily close beyond every close of the 20 days before it
    prior = closes[-21:-1]
    for side, broke, level in (("buy", closes[-1] > max(prior), max(prior)), ("sell", closes[-1] < min(prior), min(prior))):
        if broke:
            sgn = 1 if side == "buy" else -1
            entry = level + sgn * 0.1 * a                 # a retest of the broken level
            stop = level - sgn * 0.5 * a
            st, why = _entry_status(side, price, entry, stop, 0.15 * a,
                                    (last_h1.close > last_h1.open) == (side == "buy"))
            p = plan(pair, ins, "20-day breakout", side, entry, stop, entry + sgn * 2 * abs(entry - stop), st, why,
                     "The daily close broke the 20-day %s close (%s). Plan: %s the retest of the level."
                     % ("highest" if side == "buy" else "lowest", ins.round_price(level), side), "H1")
            if p:
                out.append(p)

    # flag: a strong 10-day move, then 5 days inside a tight range; enter on the break of the pause
    move = closes[-6] - closes[-16]
    box_hi = max(c.high for c in d1[-5:])
    box_lo = min(c.low for c in d1[-5:])
    if abs(move) > 3 * a and (box_hi - box_lo) < 1.2 * a:
        side = "buy" if move > 0 else "sell"
        sgn = 1 if side == "buy" else -1
        entry = box_hi if side == "buy" else box_lo
        stop = box_lo if side == "buy" else box_hi
        broken = sgn * (price - entry) > 0
        st, why = ("enter now", "price has broken out of the flag") if broken and abs(price - entry) < 0.3 * a else \
            (("wait", "waiting for a break of the flag at %s" % ins.round_price(entry)) if not broken else
             ("wait", "the break is already far from the entry: a late entry is a different trade"))
        p = plan(pair, ins, "Flag", side, entry, stop, entry + sgn * 0.5 * abs(move), st, why,
                 "A %s move of %.0f pips, then a 5-day pause of %.0f pips. Plan: %s the break of the pause."
                 % ("rising" if side == "buy" else "falling", abs(move) / ins.pip, (box_hi - box_lo) / ins.pip, side), "D1")
        if p:
            out.append(p)

    # 20-day average: the close crosses back over the 20-day simple average
    m_now, m_before = sma(closes, 20), sma(closes[:-1], 20)
    if m_now and m_before:
        for side, crossed in (("buy", closes[-2] < m_before and closes[-1] > m_now),
                              ("sell", closes[-2] > m_before and closes[-1] < m_now)):
            if crossed:
                sgn = 1 if side == "buy" else -1
                entry = m_now
                stop = (d1[-1].low if side == "buy" else d1[-1].high) - sgn * 0.1 * a
                st, why = _entry_status(side, price, entry, stop, 0.15 * a,
                                        (last_h1.close > last_h1.open) == (side == "buy"))
                p = plan(pair, ins, "20-day average", side, entry, stop, entry + sgn * 2 * abs(entry - stop), st, why,
                         "The daily close went back %s the 20-day average (%s). Plan: %s a pullback to it on H1."
                         % ("above" if side == "buy" else "below", ins.round_price(m_now), side), "H1")
                if p:
                    out.append(p)

    # RSI bounce: oversold (overbought) yesterday, today closed back up (down)
    r = rsi(closes)
    if r[-2] is not None and r[-1] is not None:
        for side, hit in (("buy", r[-2] < 30 and closes[-1] > closes[-2]),
                          ("sell", r[-2] > 70 and closes[-1] < closes[-2])):
            if hit:
                sgn = 1 if side == "buy" else -1
                entry = price
                extreme = min(c.low for c in d1[-3:]) if side == "buy" else max(c.high for c in d1[-3:])
                stop = extreme - sgn * 0.1 * a
                target = sma(closes, 20) or entry + sgn * 2 * abs(entry - stop)
                p = plan(pair, ins, "RSI bounce", side, entry, stop, target, "enter now",
                         "RSI was %.0f yesterday and today closed %s" % (r[-2], "up" if side == "buy" else "down"),
                         "%s on the daily RSI, then a turn. Plan: %s toward the 20-day average."
                         % ("Oversold" if side == "buy" else "Overbought", side), "D1")
                if p:
                    out.append(p)
    return out


# ------------------------------------------------------------------ JeaFx-style supply and demand
def supply_demand(pair, ins, h4, m15) -> list:
    if len(h4) < 60 or len(m15) < 40:
        return []
    t = trend(h4)
    if t["bias"] not in ("up", "down"):
        return []
    side = "buy" if t["bias"] == "up" else "sell"
    sgn = 1 if side == "buy" else -1
    a = atr(h4) or 0
    if a <= 0:
        return []
    # the impulse: the strongest trend-direction H4 candle in the last 30, closing near its extreme
    best, idx = 0.0, None
    for i in range(len(h4) - 30, len(h4) - 1):
        c = h4[i]
        body = sgn * (c.close - c.open)
        if body > 1.5 * a and body > best:
            best, idx = body, i
    if idx is None or idx < 1:
        return []
    # the zone: the last opposite candle before the impulse
    j = idx - 1
    while j > idx - 6 and sgn * (h4[j].close - h4[j].open) > 0:
        j -= 1
    base = h4[j]
    proximal = base.high if side == "buy" else base.low
    distal = base.low if side == "buy" else base.high
    # first return only: price has not traded back into the zone since the impulse
    after = h4[idx + 1:]
    touched = any((c.low <= proximal) if side == "buy" else (c.high >= proximal) for c in after[:-1])
    if touched:
        return []
    swing_hi, swing_lo = swings(h4[-40:])
    target_pool = [v for _, v in (swing_hi if side == "buy" else swing_lo)]
    target = (max(target_pool) if side == "buy" else min(target_pool)) if target_pool else proximal + sgn * 3 * a
    price = m15[-1].close
    last = m15[-1]
    in_zone = (distal <= price <= proximal) if side == "buy" else (proximal <= price <= distal)
    confirm = (last.close > last.open and last.close > m15[-2].high) if side == "buy" else \
        (last.close < last.open and last.close < m15[-2].low)
    if sgn * (price - distal) <= 0:
        st, why = "invalid", "price has gone through the zone"
    elif in_zone and confirm:
        st, why = "enter now", "price is in the zone and an M15 candle closed %s through the last candle" % (
            "up" if side == "buy" else "down")
    elif in_zone:
        st, why = "almost", "price is in the zone: wait for an M15 candle to confirm"
    else:
        st, why = "wait", "waiting for the first return to the zone %s – %s" % (
            ins.round_price(min(proximal, distal)), ins.round_price(max(proximal, distal)))
    p = plan(pair, ins, "Supply & demand (JeaFx style)", side, proximal, distal - sgn * 0.1 * a, target, st, why,
             "H4 trend %s; the %s zone that launched the last impulse is untouched. Plan: %s its first retest."
             % (t["bias"], "demand" if side == "buy" else "supply", side), "M15")
    return [p] if p else []


# ------------------------------------------------------------------ CRT, Candle Range Theory
def crt(pair, ins, htf, ltf, label, ltf_label) -> list:
    if len(htf) < 2 or len(ltf) < 20:
        return []
    c1, c2 = htf[-2], htf[-1]                         # the range candle and the sweep candle (both closed)
    rng = c1.high - c1.low
    if rng <= 0:
        return []
    out = []
    mid = (c1.high + c1.low) / 2
    for side, swept, back in (("buy", c2.low < c1.low, c1.low < c2.close < c1.high),
                              ("sell", c2.high > c1.high, c1.low < c2.close < c1.high)):
        if not (swept and back):
            continue
        sgn = 1 if side == "buy" else -1
        wick = c2.low if side == "buy" else c2.high
        stop = wick - sgn * 0.1 * rng
        target = c1.high if side == "buy" else c1.low
        # lower-timeframe structure shift after the sweep: a close beyond the last swing against the trade
        since = [c for c in ltf if c.open_time >= c2.open_time]
        hi, lo = swings(since or ltf[-40:], 1)
        shift_level = (hi[-1][1] if hi else None) if side == "buy" else (lo[-1][1] if lo else None)
        price = ltf[-1].close
        shifted = shift_level is not None and sgn * (ltf[-1].close - shift_level) > 0
        entry = price if shifted else (c1.low if side == "buy" else c1.high)
        if sgn * (price - stop) <= 0:
            st, why = "invalid", "price went back through the sweep"
        elif sgn * (price - mid) >= 0:
            st, why = "wait", "price is already past the 50% of the range: most of the move is gone"
        elif shifted:
            st, why = "enter now", "%s shifted structure after the sweep (closed %s %s)" % (
                ltf_label, "above" if side == "buy" else "below", ins.round_price(shift_level))
        else:
            st, why = "wait", "waiting for %s to shift structure after the sweep" % ltf_label
        p = plan(pair, ins, "CRT %s → %s" % (label, ltf_label), side, entry, stop, target, st, why,
                 "The last %s candle swept the previous %s's %s (%s) and closed back inside its range. "
                 "Target the other side (%s), 50%% at %s." % (label, label, "low" if side == "buy" else "high",
                                                             ins.round_price(c1.low if side == "buy" else c1.high),
                                                             ins.round_price(target), ins.round_price(mid)),
                 ltf_label, min_rr=1.0)
        if p:
            p["plan"]["target_50"] = ins.round_price(mid)
            out.append(p)
    return out
