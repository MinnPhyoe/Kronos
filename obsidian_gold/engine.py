"""OBSIDIAN Gold: the analysis engine.

Plain standard-library Python. Candles are dicts: {"t": epoch seconds UTC,
"o", "h", "l", "c", "v"}. Every function here is pure: give it candles,
get numbers back. The server and the tests both call these.

The strategy ("Gold Pulse") is new and written for XAUUSD:

  1. Bias from the higher timeframes: H1 EMA50 against EMA200, and the H4
     close against its EMA50. Both up = bullish, both down = bearish,
     otherwise neutral (no trades).
  2. Entries on M15, only inside the London or New York window.
       Pullback: a candle dips to the EMA20 and closes back on the trend
       side with a rejection (close in the outer 40 % of its range), RSI
       not stretched.
       Breakout: a candle closes beyond the last 20-candle high or low with
       a body of at least 0.6 ATR.
  3. Stop beyond the signal candle and the 5-candle swing, plus 0.3 ATR,
     kept between 0.8 and 3 ATR. Target 1 at 1R, target 2 at 2R.
  4. Every signal is checked against history (the same rule on the loaded
     candles) and against a forward simulation of the next hours.

None of this is financial advice. It is arithmetic on past prices.
"""

import math
import random
from datetime import datetime, timezone

M15, H1, H4, D1 = 900, 3600, 14400, 86400
TF_SECONDS = {"M5": 300, "M15": M15, "H1": H1, "H4": H4, "D1": D1}

DEFAULTS = {
    "london": ["07:00", "11:00"],      # UTC
    "newyork": ["12:00", "16:00"],     # UTC
    "asia": ["00:00", "06:00"],        # UTC, used for the Asian range
    "ema_fast": 20, "ema_mid": 50, "ema_slow": 200,
    "rsi_len": 14, "atr_len": 14,
    "breakout_len": 20, "breakout_body_atr": 0.6,
    "pullback_touch_atr": 0.25,
    "stop_buffer_atr": 0.3, "stop_min_atr": 0.8, "stop_max_atr": 3.0,
    "tp1_r": 1.0, "tp2_r": 2.0,
    "max_hold_bars": 32,               # 8 hours of M15
    "contract_oz": 100,                # 1 standard lot of XAUUSD = 100 oz
    "lot_step": 0.01, "min_lot": 0.01, "max_lot": 50.0,
}


# ---------------------------------------------------------------- basics

def utc(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


def minutes_of_day(t):
    d = utc(t)
    return d.hour * 60 + d.minute


def clock(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def in_window(t, window):
    a, b = clock(window[0]), clock(window[1])
    m = minutes_of_day(t)
    return a <= m < b if a <= b else (m >= a or m < b)


def session_of(t, cfg=DEFAULTS):
    if in_window(t, cfg["london"]):
        return "London"
    if in_window(t, cfg["newyork"]):
        return "New York"
    if in_window(t, cfg["asia"]):
        return "Asia"
    return None


def resample(candles, seconds):
    """Group candles into buckets of `seconds`, aligned to UTC."""
    out = []
    for c in candles:
        b = c["t"] - c["t"] % seconds
        if out and out[-1]["t"] == b:
            o = out[-1]
            o["h"] = max(o["h"], c["h"])
            o["l"] = min(o["l"], c["l"])
            o["c"] = c["c"]
            o["v"] = o.get("v", 0) + c.get("v", 0)
        else:
            out.append({"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v", 0)})
    return out


# ------------------------------------------------------------ indicators

def ema(values, n):
    out, k, e = [], 2.0 / (n + 1), None
    for i, v in enumerate(values):
        if e is None:
            if i + 1 < n:
                out.append(None)
                continue
            e = sum(values[i + 1 - n:i + 1]) / n
        else:
            e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(candles, n=14):
    out = [None] * len(candles)
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["h"] - c["l"]
        else:
            p = candles[i - 1]["c"]
            tr = max(c["h"] - c["l"], abs(c["h"] - p), abs(c["l"] - p))
        trs.append(tr)
        if i + 1 == n:
            out[i] = sum(trs) / n
        elif i + 1 > n:
            out[i] = (out[i - 1] * (n - 1) + tr) / n
    return out


def indicators(candles, cfg=DEFAULTS):
    closes = [c["c"] for c in candles]
    return {
        "ema20": ema(closes, cfg["ema_fast"]),
        "ema50": ema(closes, cfg["ema_mid"]),
        "ema200": ema(closes, cfg["ema_slow"]),
        "rsi": rsi(closes, cfg["rsi_len"]),
        "atr": atr(candles, cfg["atr_len"]),
    }


# ------------------------------------------------------------------ bias

def bias_at(h1, h4, t, cfg=DEFAULTS):
    """Bias using only H1/H4 candles that had closed by time t."""
    h1c = [c for c in h1 if c["t"] + H1 <= t]
    h4c = [c for c in h4 if c["t"] + H4 <= t]
    return _bias(h1c, h4c, cfg)


def _bias(h1c, h4c, cfg=DEFAULTS):
    if len(h1c) < cfg["ema_slow"] or len(h4c) < cfg["ema_mid"]:
        return {"dir": "neutral", "h1": "unknown", "h4": "unknown", "why": "Not enough history yet."}
    e50 = ema([c["c"] for c in h1c], cfg["ema_mid"])[-1]
    e200 = ema([c["c"] for c in h1c], cfg["ema_slow"])[-1]
    h4e = ema([c["c"] for c in h4c], cfg["ema_mid"])[-1]
    h1d = "bullish" if e50 > e200 else "bearish"
    h4d = "bullish" if h4c[-1]["c"] > h4e else "bearish"
    d = h1d if h1d == h4d else "neutral"
    why = ("H1 EMA50 %s EMA200, H4 close %s its EMA50."
           % ("above" if h1d == "bullish" else "below", "above" if h4d == "bullish" else "below"))
    return {"dir": d, "h1": h1d, "h4": h4d, "why": why}


class BiasTrack:
    """Bias at many times without recomputing EMAs each time (for backtests)."""

    def __init__(self, h1, h4, cfg=DEFAULTS):
        self.cfg = cfg
        self.h1, self.h4 = h1, h4
        self.h1e50 = ema([c["c"] for c in h1], cfg["ema_mid"])
        self.h1e200 = ema([c["c"] for c in h1], cfg["ema_slow"])
        self.h4e50 = ema([c["c"] for c in h4], cfg["ema_mid"])

    def at(self, t):
        i = _last_closed(self.h1, H1, t)
        j = _last_closed(self.h4, H4, t)
        if i is None or j is None or self.h1e200[i] is None or self.h4e50[j] is None:
            return "neutral"
        a = self.h1e50[i] > self.h1e200[i]
        b = self.h4[j]["c"] > self.h4e50[j]
        return "bullish" if a and b else "bearish" if (not a and not b) else "neutral"


def _last_closed(cs, sec, t):
    lo, hi, ans = 0, len(cs) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if cs[mid]["t"] + sec <= t:
            ans, lo = mid, mid + 1
        else:
            hi = mid - 1
    return ans


# --------------------------------------------------------------- signals

def detect(m15, i, ind, bias, cfg=DEFAULTS):
    """Is there a Gold Pulse signal on the M15 candle at index i (closed)?"""
    if bias not in ("bullish", "bearish"):
        return None
    c = m15[i]
    a, e20, r = ind["atr"][i], ind["ema20"][i], ind["rsi"][i]
    if a is None or e20 is None or r is None or a <= 0 or i < cfg["breakout_len"] + 5:
        return None
    end = c["t"] + M15
    sess = session_of(c["t"], cfg)
    if sess not in ("London", "New York"):
        return None
    long = bias == "bullish"
    rng = c["h"] - c["l"] or 1e-9
    kind = None
    prior = m15[i - cfg["breakout_len"]:i]
    hi, lo = max(p["h"] for p in prior), min(p["l"] for p in prior)
    body = abs(c["c"] - c["o"])
    if long and c["c"] > hi and c["c"] > c["o"] and body >= cfg["breakout_body_atr"] * a:
        kind = "Breakout"
    elif not long and c["c"] < lo and c["c"] < c["o"] and body >= cfg["breakout_body_atr"] * a:
        kind = "Breakout"
    elif long and c["l"] <= e20 + cfg["pullback_touch_atr"] * a and c["c"] > e20 \
            and c["c"] > c["o"] and (c["c"] - c["l"]) / rng >= 0.6 and 40 <= r <= 65:
        kind = "Pullback"
    elif not long and c["h"] >= e20 - cfg["pullback_touch_atr"] * a and c["c"] < e20 \
            and c["c"] < c["o"] and (c["h"] - c["c"]) / rng >= 0.6 and 35 <= r <= 60:
        kind = "Pullback"
    if not kind:
        return None
    swing = m15[max(0, i - 4):i + 1]
    entry = c["c"]
    if long:
        raw = min(min(s["l"] for s in swing), c["l"]) - cfg["stop_buffer_atr"] * a
        dist = entry - raw
    else:
        raw = max(max(s["h"] for s in swing), c["h"]) + cfg["stop_buffer_atr"] * a
        dist = raw - entry
    dist = min(max(dist, cfg["stop_min_atr"] * a), cfg["stop_max_atr"] * a)
    sgn = 1 if long else -1
    return {
        "t": end, "candle_t": c["t"], "side": "buy" if long else "sell", "kind": kind,
        "session": sess, "entry": round(entry, 2),
        "stop": round(entry - sgn * dist, 2),
        "tp1": round(entry + sgn * dist * cfg["tp1_r"], 2),
        "tp2": round(entry + sgn * dist * cfg["tp2_r"], 2),
        "risk": round(dist, 2), "atr": round(a, 2), "rsi": round(r, 1),
    }


def walk(m15, start, sig, cfg=DEFAULTS):
    """Follow a signal forward from candle index `start`. Stop is checked first
    inside a candle (the cautious assumption)."""
    long = sig["side"] == "buy"
    tp1_hit = False
    for k in range(start, min(len(m15), start + cfg["max_hold_bars"])):
        c = m15[k]
        hit_sl = c["l"] <= sig["stop"] if long else c["h"] >= sig["stop"]
        hit_tp2 = c["h"] >= sig["tp2"] if long else c["l"] <= sig["tp2"]
        hit_tp1 = c["h"] >= sig["tp1"] if long else c["l"] <= sig["tp1"]
        if hit_sl:
            return {"result": "tp1_then_stop" if tp1_hit else "stop", "r": 0.0 if tp1_hit else -1.0,
                    "bars": k - start + 1, "exit_t": c["t"], "tp1": tp1_hit}
        if hit_tp1:
            tp1_hit = True
        if hit_tp2:
            return {"result": "tp2", "r": cfg["tp2_r"], "bars": k - start + 1, "exit_t": c["t"], "tp1": True}
    end = min(len(m15), start + cfg["max_hold_bars"])
    if end - start < cfg["max_hold_bars"]:
        return {"result": "open", "r": None, "bars": end - start, "exit_t": None, "tp1": tp1_hit}
    last = m15[end - 1]["c"]
    r = (last - sig["entry"]) / sig["risk"] * (1 if long else -1)
    return {"result": "timeout", "r": round(r, 2), "bars": cfg["max_hold_bars"], "exit_t": m15[end - 1]["t"],
            "tp1": tp1_hit}


def scan(m15, h1, h4, cfg=DEFAULTS):
    """Every signal in the history, with its outcome. Signals don't overlap:
    a new one waits until the last one is finished."""
    ind = indicators(m15, cfg)
    track = BiasTrack(h1, h4, cfg)
    out, busy_until = [], -1
    for i in range(len(m15)):
        if i <= busy_until:
            continue
        b = track.at(m15[i]["t"] + M15)
        s = detect(m15, i, ind, b, cfg)
        if not s:
            continue
        s["outcome"] = walk(m15, i + 1, s, cfg)
        out.append(s)
        busy_until = i + s["outcome"]["bars"]
    return out


def stats(signals):
    done = [s for s in signals if s["outcome"]["r"] is not None]
    by = {}
    for key in ("all", "buy", "sell", "Breakout", "Pullback", "London", "New York"):
        if key == "all":
            g = done
        elif key in ("buy", "sell"):
            g = [s for s in done if s["side"] == key]
        elif key in ("Breakout", "Pullback"):
            g = [s for s in done if s["kind"] == key]
        else:
            g = [s for s in done if s["session"] == key]
        rs = [s["outcome"]["r"] for s in g]
        wins = sum(1 for r in rs if r > 0)
        eq, peak, dd = 0.0, 0.0, 0.0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        by[key] = {
            "trades": len(rs), "wins": wins,
            "win_rate": round(wins / len(rs), 3) if rs else None,
            "tp1_rate": round(sum(1 for s in g if s["outcome"]["tp1"]) / len(g), 3) if g else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "net_r": round(sum(rs), 2), "max_dd_r": round(dd, 2),
        }
    curve, eq = [], 0.0
    for s in done:
        eq += s["outcome"]["r"]
        curve.append({"t": s["t"], "r": round(eq, 2)})
    by["curve"] = curve
    return by


# ---------------------------------------------------------------- levels

def levels(m15, cfg=DEFAULTS):
    """Previous day high/low, today's Asian range, nearby round numbers and
    recent swing points."""
    if not m15:
        return []
    last = m15[-1]
    day0 = last["t"] - last["t"] % D1
    out = []
    prev = [c for c in m15 if day0 - D1 <= c["t"] < day0]
    if not prev:  # Monday: look back further
        older = [c for c in m15 if c["t"] < day0]
        if older:
            d = older[-1]["t"] - older[-1]["t"] % D1
            prev = [c for c in older if c["t"] >= d]
    if prev:
        out.append({"name": "Previous day high", "price": round(max(c["h"] for c in prev), 2), "kind": "pd"})
        out.append({"name": "Previous day low", "price": round(min(c["l"] for c in prev), 2), "kind": "pd"})
    asia = [c for c in m15 if c["t"] >= day0 and in_window(c["t"], cfg["asia"])]
    if asia:
        out.append({"name": "Asia high", "price": round(max(c["h"] for c in asia), 2), "kind": "asia"})
        out.append({"name": "Asia low", "price": round(min(c["l"] for c in asia), 2), "kind": "asia"})
    p = last["c"]
    step = 25 if p > 1000 else 5
    base = math.floor(p / step) * step
    for k in (-1, 0, 1, 2):
        lv = base + k * step
        out.append({"name": "Round %s" % _fmt_round(lv), "price": float(lv), "kind": "round"})
    # swing points: a high/low that is the extreme of the 5 candles each side, last 3 of each
    hs, ls = [], []
    for i in range(5, len(m15) - 5):
        w = m15[i - 5:i + 6]
        if m15[i]["h"] == max(c["h"] for c in w):
            hs.append(m15[i]["h"])
        if m15[i]["l"] == min(c["l"] for c in w):
            ls.append(m15[i]["l"])
    for v in hs[-2:]:
        out.append({"name": "Swing high", "price": round(v, 2), "kind": "swing"})
    for v in ls[-2:]:
        out.append({"name": "Swing low", "price": round(v, 2), "kind": "swing"})
    out.sort(key=lambda x: -x["price"])
    merged = []
    for lv in out:  # one row per price: "Asia high, Swing high"
        if merged and abs(merged[-1]["price"] - lv["price"]) < 0.05:
            if lv["name"] not in merged[-1]["name"]:
                merged[-1]["name"] += ", " + lv["name"]
            continue
        merged.append(dict(lv))
    return merged


def _fmt_round(v):
    return ("%.0f" % v) if v == int(v) else ("%.2f" % v)


# ------------------------------------------------------------ projection

def project(m15, plan=None, bars=16, paths=2000, seed=7, lookback_days=60):
    """Forward simulation: each future M15 candle's move is drawn from the
    real moves at the same time of day over the last `lookback_days`
    (scaled to today's volatility). Gives a cone (10/25/50/75/90th
    percentile closes) and, for a plan, how often target or stop came first.
    """
    if len(m15) < 200:
        return None
    rng = random.Random(seed)
    last = m15[-1]
    cut = last["t"] - lookback_days * D1
    pool = {}
    hist = [c for c in m15 if c["t"] >= cut]
    for c in hist:
        if c["o"] <= 0:
            continue
        slot = minutes_of_day(c["t"]) // 15
        pool.setdefault(slot, []).append((
            math.log(c["c"] / c["o"]),
            math.log(c["h"] / max(c["o"], c["c"])),
            math.log(min(c["o"], c["c"]) / c["l"]) if c["l"] > 0 else 0.0,
        ))
    allm = [m for v in pool.values() for m in v]
    if not allm:
        return None
    # volatility scaling: recent 1-day realized vs the pool's
    recent = [abs(math.log(c["c"] / c["o"])) for c in m15[-96:] if c["o"] > 0]
    poolv = sum(abs(m[0]) for m in allm) / len(allm) or 1e-9
    scale = min(max((sum(recent) / len(recent)) / poolv, 0.5), 2.0) if recent else 1.0
    times = [last["t"] + M15 * (k + 1) for k in range(bars)]
    slots = [minutes_of_day(t) // 15 for t in times]
    finals = [[] for _ in range(bars)]
    tp = sl = neither = 0
    long = plan and plan["side"] == "buy"
    for _ in range(paths):
        p = last["c"]
        state = None
        for k in range(bars):
            src = pool.get(slots[k]) or allm
            ret, up, dn = src[rng.randrange(len(src))]
            o = p
            p = o * math.exp(ret * scale)
            hi = max(o, p) * math.exp(up * scale)
            lo = min(o, p) / math.exp(dn * scale)
            finals[k].append(p)
            if plan and state is None:
                hs = lo <= plan["stop"] if long else hi >= plan["stop"]
                ht = hi >= plan["tp2"] if long else lo <= plan["tp2"]
                if hs:
                    state = "sl"
                elif ht:
                    state = "tp"
        if plan:
            if state == "tp":
                tp += 1
            elif state == "sl":
                sl += 1
            else:
                neither += 1
    cone = []
    for k in range(bars):
        f = sorted(finals[k])
        q = lambda x: round(f[min(len(f) - 1, int(x * len(f)))], 2)
        cone.append({"t": times[k], "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90)})
    endp = sorted(finals[-1])
    up = sum(1 for v in endp if v > last["c"]) / len(endp)
    res = {"bars": bars, "paths": paths, "seed": seed, "from": round(last["c"], 2), "cone": cone,
           "prob_up": round(up, 3), "vol_scale": round(scale, 2), "method": "Session-matched replay"}
    if plan:
        res["plan_odds"] = {"tp2_first": round(tp / paths, 3), "stop_first": round(sl / paths, 3),
                            "neither": round(neither / paths, 3)}
    return res


# ---------------------------------------------------------------- sizing

def size(balance, risk_pct, risk_per_oz, cfg=DEFAULTS):
    """Lots so that a stop-out costs at most risk_pct of balance."""
    if not balance or not risk_pct or not risk_per_oz or risk_per_oz <= 0:
        return None
    budget = balance * risk_pct / 100.0
    per_lot = risk_per_oz * cfg["contract_oz"]
    raw = budget / per_lot
    step = cfg["lot_step"]
    lots = math.floor(raw / step + 1e-9) * step
    lots = min(lots, cfg["max_lot"])
    if lots < cfg["min_lot"]:
        return {"lots": 0.0, "budget": round(budget, 2), "loss_at_stop": 0.0,
                "note": "The stop is too wide for this risk. Even %.2f lots would lose %.2f." %
                        (cfg["min_lot"], cfg["min_lot"] * per_lot)}
    lots = round(lots, 2)
    return {"lots": lots, "budget": round(budget, 2), "loss_at_stop": round(lots * per_lot, 2),
            "per_dollar_move": round(lots * cfg["contract_oz"], 2), "note": ""}


# ------------------------------------------------------------- the clock

def next_session(now_t, cfg=DEFAULTS):
    """The current session (if any) and the next London/NY open."""
    cur = session_of(now_t, cfg)
    best = None
    for name, key in (("London", "london"), ("New York", "newyork")):
        start = clock(cfg[key][0])
        day0 = now_t - now_t % D1
        for d in range(0, 4):
            t = day0 + d * D1 + start * 60
            if t > now_t and utc(t).weekday() < 5:
                if best is None or t < best[1]:
                    best = (name, t)
                break
    return {"current": cur, "trade_window": cur in ("London", "New York"),
            "next": best[0] if best else None, "next_t": best[1] if best else None}


# ------------------------------------------------------------- the whole

def analyse(m15, cfg=DEFAULTS, now_t=None, balance=None, risk_pct=None, plan_paths=2000):
    """Everything the dashboard shows, from M15 candles."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    if len(m15) < 50:
        return {"ok": False, "why": "Need at least 50 M15 candles, have %d." % len(m15)}
    now_t = now_t or (m15[-1]["t"] + M15)
    h1, h4 = resample(m15, H1), resample(m15, H4)
    ind = indicators(m15, cfg)
    b = _bias([c for c in h1 if c["t"] + H1 <= now_t], [c for c in h4 if c["t"] + H4 <= now_t], cfg)
    history = scan(m15, h1, h4, cfg)
    st = stats(history)
    # the live signal: the newest signal, if its candle is the latest closed
    # one, or it is still open (not stopped, not at target, not timed out)
    live = None
    if history:
        s = history[-1]
        if s["outcome"]["result"] == "open":
            live = s
    last_i = len(m15) - 1
    checks = _checks(m15, last_i, ind, b["dir"], cfg)
    plan = live or None
    proj = project(m15, plan=plan, paths=plan_paths)
    if live and balance and risk_pct:
        live["size"] = size(balance, risk_pct, live["risk"], cfg)
    last = m15[-1]
    return {
        "ok": True, "price": round(last["c"], 2), "last_t": last["t"], "first_t": m15[0]["t"],
        "change_day": _day_change(m15),
        "bias": b, "checks": checks, "signal": live,
        "history": history[-60:], "stats": st,
        "levels": levels(m15, cfg), "projection": proj,
        "clock": next_session(now_t, cfg),
        "atr": round(ind["atr"][-1], 2) if ind["atr"][-1] else None,
        "rsi": round(ind["rsi"][-1], 1) if ind["rsi"][-1] else None,
        "ema20": round(ind["ema20"][-1], 2) if ind["ema20"][-1] else None,
    }


def _day_change(m15):
    last = m15[-1]
    day0 = last["t"] - last["t"] % D1
    prev = [c for c in m15 if c["t"] < day0]
    if not prev:
        return None
    ref = prev[-1]["c"]
    return {"abs": round(last["c"] - ref, 2), "pct": round((last["c"] / ref - 1) * 100, 2)}


def _checks(m15, i, ind, bias, cfg):
    """Where the latest candle stands against each rule (for the Signal page)."""
    c = m15[i]
    a, e20, r = ind["atr"][i], ind["ema20"][i], ind["rsi"][i]
    sess = session_of(c["t"] + M15, cfg)
    out = [{"q": "Is the higher timeframe trend clear?",
            "ok": bias in ("bullish", "bearish"), "detail": "Bias is %s." % bias}]
    out.append({"q": "Is it London or New York now?", "ok": sess in ("London", "New York"),
                "detail": ("Inside %s." % sess) if sess in ("London", "New York") else
                "Outside the trading windows (%s)." % (sess or "off hours")})
    if a and e20 and r is not None:
        dist = (c["c"] - e20) / a
        out.append({"q": "Is price near the EMA20 (pullback zone)?", "ok": abs(dist) <= 0.6,
                    "detail": "%.2f ATR from the EMA20 (%.2f)." % (dist, e20)})
        prior = m15[max(0, i - cfg["breakout_len"]):i]
        if prior:
            hi, lo = max(p["h"] for p in prior), min(p["l"] for p in prior)
            out.append({"q": "Has price broken the 20-candle range?",
                        "ok": c["c"] > hi or c["c"] < lo,
                        "detail": "Range %.2f to %.2f, close %.2f." % (lo, hi, c["c"])})
        ok_r = (40 <= r <= 65) if bias == "bullish" else (35 <= r <= 60) if bias == "bearish" else False
        out.append({"q": "Is RSI in the allowed band?", "ok": ok_r, "detail": "RSI %.1f." % r})
    return out
