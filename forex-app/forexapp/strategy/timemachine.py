"""The Time Machine (trader-timemachine, ported to forex).

Before a trade, it asks: if the next hours play out like this pair's own
last 60 days, how often does the plan hit the target first, the stop first,
or neither?

1. The pool: every 5-minute candle of the past ``lookback_days`` becomes four
   ratios to the candle before it (open, high, low, close over the previous
   close), grouped by time of day in the session's local time.
2. One replay: start at the entry price; for each candle ahead, pick a past
   candle from the same time of day (plus or minus 2 slots) at random and
   scale it to the current price. Check the stop first, then the target. A
   candle that touches both counts as the stop.
3. Many replays give the odds: target first, stop first, neither (still open
   at the end of the horizon), and the average R.
4. The verdict is one line from the numbers, weighed by the target's R: a
   1R target needs to come first in half of the replays that hit a side.

It is a simulation from recent candles, not a prediction. The draws come from
``random.Random(seed)``: same plan, same data, same seed, same numbers.
"""
from __future__ import annotations

import random
import statistics
from zoneinfo import ZoneInfo

from .sessions import local_slot

SLOTS = 288                  # 5-minute slots in a day
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40


def build_pool(candles_5m: list, tz: ZoneInfo, before: int, lookback_days: int = 60) -> dict:
    """slot -> [(o, h, l, c) / previous close] from candles that opened in
    [before - lookback_days, before). Only consecutive candles count."""
    start = before - lookback_days * 86400
    pool: dict[int, list] = {}
    prev = None
    for c in candles_5m:
        if c.open_time >= before:
            break
        if c.open_time >= start and prev is not None and c.open_time - prev.open_time == 300:
            pc = prev.close
            pool.setdefault(local_slot(c.open_time, tz), []).append((c.open / pc, c.high / pc, c.low / pc, c.close / pc))
        prev = c
    return pool


def add_synthetic(pool: dict, forecast, tz: ZoneInfo) -> int:
    """Phase 7: add a forecast's paths to the pool as extra candles."""
    n = 0
    for path in forecast.paths:
        pc = forecast.last_close
        for t, (o, h, l, c) in zip(forecast.times, path):
            pool.setdefault(local_slot(t, tz), []).append((o / pc, h / pc, l / pc, c / pc))
            pc = c
            n += 1
    return n


def pool_size(pool: dict) -> int:
    return sum(len(v) for v in pool.values())


def _pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool: dict, side: str, start_price: float, entry: float, stop: float, target: float,
             start_time: int, n_bars: int, spread: float, tz: ZoneInfo, replays: int = 2000, seed: int = 19,
             window: int = 2) -> dict:
    """Replays of the ``n_bars`` 5-minute candles opening at ``start_time``.

    ``start_price`` is the bid at the entry (the path starts there); ``entry`` is
    the fill (the ask for a buy). Buy stops and targets are checked on the bid;
    sell stops and targets on the ask (bid + spread).
    """
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        raise ValueError("the stop is on the wrong side of the entry")
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    slots = [local_slot(start_time + 300 * j, tz) for j in range(n_bars)]
    cands = []
    for s in slots:
        rows = []
        for d in range(-window, window + 1):
            rows.extend(pool.get((s + d) % SLOTS, ()))
        cands.append(rows)
    if any(len(r) == 0 for r in cands):
        raise ValueError("the pool has no candles for some times of day (need more history)")
    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, Rs = [], [], []
    for _ in range(replays):
        p = start_price
        outcome = None
        for rows in cands:
            o, h, l, c = choice(rows)
            hi, lo = p * h, p * l
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi + spread >= stop:
                        outcome = "stop"
                    elif lo + spread <= target:
                        outcome = "target"
            p = p * c
        outcomes.append(outcome or "neither")
        closes.append(p)
        if outcome == "target":
            Rs.append(r_target)
        elif outcome == "stop":
            Rs.append(-1.0)
        else:
            Rs.append(((p - entry) if long else (entry - (p + spread))) / risk)
    cnt = {k: outcomes.count(k) for k in ("target", "stop", "neither")}
    srt = sorted(closes)
    pt, ps = cnt["target"] / replays, cnt["stop"] / replays
    mc = {"replays": replays, "seed": seed, "window": window, "bars": n_bars,
          "p_target": round(pt, 4), "p_stop": round(ps, 4), "p_neither": round(cnt["neither"] / replays, 4),
          "odds": round(pt / (pt + ps), 4) if pt + ps > 0 else None,
          "break_even": round(1.0 / (1.0 + r_target), 4), "r_at_target": r_target,
          "avg_R": round(statistics.mean(Rs), 3), "median_close": _pct(srt, 0.5), "p5_close": _pct(srt, 0.05),
          "p95_close": _pct(srt, 0.95), "pool": pool_size(pool)}
    mc["verdict"] = verdict(mc)
    mc["notes"] = verdict_notes(mc)
    return mc


def lean(mc: dict):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc: dict) -> str:
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop within the horizon."
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc: dict) -> list:
    notes = []
    lv = lean(mc)
    if mc.get("p_neither", 0) > MANY_NEITHER and lv is not None:
        notes.append("%.1f %% of the replays hit neither within the horizon; the price then decides those."
                     % (100 * mc["p_neither"]))
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes
