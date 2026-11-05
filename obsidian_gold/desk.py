"""OBSIDIAN Gold: the day-trader-skill tools, pointed at gold.

This file has no strategy of its own. It loads the tools from your clone of
day-trader-skill and runs them on XAUUSD 5-minute bars:

  trader-entry        opening-range breakout: waiting / triggered / expired
  trader-exit         stop at the range midpoint (or the far side), target in R
  trader-timemachine  10,000 replays of the rest of the session + every past
                      session the same rule fired
  trader-budget       size from your max loss per trade (ounces -> lots)
  trader-alerts       stop, target and flat-by-close alerts

Those tools are written for long stock trades in the New York session (9:30
to 16:00 ET). Gold also needs sells, so a sell is run as a buy on a mirror
image of the chart (price p becomes M - p): a close below the range low is a
close above the mirrored range high. Every number shown is mirrored back.

The forecast is the Kronos model from this repo (kronos_forecast below).
"""

import datetime
import importlib.util
import json
import math
import os
import random
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOL = "XAUUSD"          # what the day-trader tools see; Yahoo's GC=F / XAUUSD=X is relabelled
EPOCH = datetime.datetime(1970, 1, 1)


class DeskError(Exception):
    pass


# ------------------------------------------------------------- the tools

def find_day_trader():
    """Your day-trader-skill clone: $DAY_TRADER_PATH, or next to Kronos."""
    cands = [os.environ.get("DAY_TRADER_PATH"),
             os.path.join(os.path.dirname(os.path.dirname(HERE)), "day-trader-skill"),
             os.path.join(os.path.dirname(HERE), "day-trader-skill"),
             os.path.expanduser("~/day-trader-skill")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "trader-entry", "entry.py")):
            return os.path.abspath(c)
    raise DeskError("day-trader-skill not found. Clone it next to Kronos "
                    "(git clone https://github.com/MinnPhyoe/day-trader-skill) or set DAY_TRADER_PATH.")


def _load(root, folder, name):
    spec = importlib.util.spec_from_file_location("dts_" + name, os.path.join(root, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tools:
    def __init__(self, root=None):
        self.root = root or find_day_trader()
        self.entry = _load(self.root, "trader-entry", "entry")
        self.exit = _load(self.root, "trader-exit", "exit")
        self.tm = _load(self.root, "trader-timemachine", "timemachine")
        self.budget = _load(self.root, "trader-budget", "size")
        self.alerts = _load(self.root, "trader-alerts", "alerts")
        self.errors = (self.entry.EntryError, self.exit.PlanError, self.tm.TMError,
                       self.budget.SizeError, self.alerts.AlertError)

    def fetch(self, symbol="GC=F"):
        """60 days of 5-minute bars from Yahoo, with trader-entry's own fetcher."""
        return self.entry.yahoo_get(self.entry.chart_path(symbol, "60d", "5m"))


# ---------------------------------------------------------- chart helpers

def relabel(text):
    """Yahoo JSON with the symbol set to XAUUSD (the tools check symbols)."""
    doc = json.loads(text)
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        err = ((doc.get("chart") or {}).get("error") or {}).get("description") or "no data"
        raise DeskError("Yahoo sent no chart: %s" % err)
    res[0].setdefault("meta", {})["symbol"] = SYMBOL
    return doc


def mirror_value(m, p):
    return None if p is None else round(m - p, 2)


def mirror(doc, m):
    """The chart turned upside down around m: highs become lows."""
    d = json.loads(json.dumps(doc))
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    o, h, l, c = q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", [])
    q["open"] = [mirror_value(m, x) for x in o]
    q["close"] = [mirror_value(m, x) for x in c]
    q["high"] = [mirror_value(m, x) for x in l]
    q["low"] = [mirror_value(m, x) for x in h]
    meta = r.get("meta", {})
    for k in ("previousClose", "chartPreviousClose", "regularMarketPrice"):
        if isinstance(meta.get(k), (int, float)):
            meta[k] = mirror_value(m, meta[k])
    return d


def mirror_point(doc):
    """An integer far enough above every price that mirrored prices stay positive."""
    q = doc["chart"]["result"][0]["indicators"]["quote"][0]
    hi = max([x for x in q.get("high", []) if x is not None] or [1])
    return int(math.ceil(hi)) * 2 + 100


def all_bars(doc):
    """Every 5-minute bar (24h), oldest first: {t, o, h, l, c, v}."""
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        vals = [q.get(k, [None])[i] if i < len(q.get(k, [])) else None for k in ("open", "high", "low", "close")]
        if t is None or None in vals or int(t) % 300:
            continue
        v = (q.get("volume") or [])
        out.append({"t": int(t), "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3],
                    "v": (v[i] if i < len(v) else 0) or 0})
    return out


def utc_now():
    return datetime.datetime.utcnow().replace(microsecond=0)


def ts_of(dt):
    return int((dt - EPOCH).total_seconds())


# ------------------------------------------------------------- the desk

DEFAULTS = {
    "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "balance": 5000.0, "risk_pct": 1.0, "leverage": 100, "replays": 10000,
}


def analyse(tools, text, now_utc, settings=None):
    """Everything the page shows, from one Yahoo 5-minute chart (60 days)."""
    s = dict(DEFAULTS, **(settings or {}))
    doc = relabel(text)
    m = mirror_point(doc)
    side_docs = {"buy": doc, "sell": mirror(doc, m)}
    back = {"buy": lambda p: p, "sell": lambda p: None if p is None else round(m - p, 2)}
    cfg = {"no_entries_before": s["no_entries_before"], "no_entries_after": s["no_entries_after"]}

    E = tools.entry
    res = {}
    for side, d in side_docs.items():
        ch = E.parse_chart(json.dumps(d), SYMBOL)
        res[side] = E.evaluate(ch, now_utc, setup="orb", or_minutes=int(s["or_minutes"]), interval="5m", cfg=cfg)
    base = res["buy"]
    out = {
        "now_et": base["now"], "date": base["date"], "phase": base["phase"], "reason": base["reason"],
        "opens": base.get("opens"), "closes": base.get("closes"),
        "no_entries_before": base.get("no_entries_before"), "no_entries_after": base.get("no_entries_after"),
        "status": base["status"], "side": None, "range": None, "plan": None, "odds": None, "size": None,
        "watch": None, "history": None, "next_open": base.get("next_open"),
    }
    orng = base.get("opening_range")
    if orng:
        out["range"] = {"high": orng["high"], "low": orng["low"], "minutes": orng["minutes"],
                        "complete": orng["complete"]}

    trig = [(sd, r) for sd, r in res.items() if r["status"] == "triggered"]
    if trig:
        side, r = min(trig, key=lambda x: x[1]["trigger"]["bar_index"])
        out.update(status="triggered", side=side, reason=_reason(side, r, back[side]))
        out["trigger"] = {"time": r["trigger"]["time"], "close_time": r["trigger"]["close_time"],
                          "price": back[side](r["trigger"]["price"]), "minutes_since": r.get("minutes_since")}
    elif base["status"] in ("waiting",) and base["phase"] == "open" and orng:
        last = base.get("last") or {}
        lc = last.get("close")
        out["status"] = "waiting"
        out["reason"] = ("Watching for a 5-minute close above %.2f (buy) or below %.2f (sell). Last close %s."
                         % (orng["high"], orng["low"], "%.2f" % lc if lc else "–"))
    elif base["status"] == "expired" and all(r["status"] == "expired" for r in res.values()):
        out["reason"] = "No close beyond the opening range by %s. No trade today." % base.get("no_entries_after")

    # past sessions: the same rule on both sides, from trader-timemachine
    tm = tools.tm
    result = tm.chart_result(json.dumps(doc), SYMBOL)
    meta, bars, _ = tm.parse_bars(result, now_utc)
    days = tm.split_sessions(bars)
    sessions, _short = tm.full_sessions(days, out["date"])
    last = tm.SESSION_BARS - 1 - 1  # until 15:55 bar close (16:00)
    flat_min = tm.parse_clock(s["flat_by"], "flat_by")
    last = min(last, (flat_min - tm.OPEN_MIN) // 5 - 1)
    if sessions:
        hist = {}
        for side in ("buy", "sell"):
            far = "or_low" if side == "buy" else "or_high"
            rule = {"setup": "orb", "or_minutes": int(s["or_minutes"]),
                    "stop_rule": "or_mid" if s["stop_rule"] == "or_mid" else far, "target_r": float(s["target_r"])}
            hist[side] = tm.history_replay(sessions, rule, last, side)
        out["history"] = _merge_history(hist)

    if out["side"]:
        side = out["side"]
        r = res[side]
        rr = r["opening_range"]
        X = tools.exit
        stop_rule = "or_mid" if s["stop_rule"] == "or_mid" else "or_low"
        try:
            p = X.compute(Decimal(str(r["trigger"]["price"])), stop_rule, None, "r", Decimal(str(s["target_r"])),
                          or_high=rr["high"], or_low=rr["low"])
        except X.PlanError as e:
            out["plan_error"] = str(e)
            return out
        entry, stop, target = (back[side](float(p[k])) for k in ("entry", "stop", "target"))
        out["plan"] = {"side": side, "entry": entry, "stop": stop, "target": target,
                       "risk": round(abs(entry - stop), 2), "target_r": float(p["target_r"]),
                       "stop_label": p["stop_label"].replace("low", "far side") if side == "sell" else p["stop_label"],
                       "entry_time": r["trigger"]["close_time"], "flat_by": s["flat_by"]}
        # size, with trader-budget (long view: the mirrored plan for a sell)
        long_plan = {"symbol": SYMBOL, "side": "buy", "setup": "orb", "date": out["date"],
                     "entry": float(p["entry"]), "stop": float(p["stop"]), "target": float(p["target"]),
                     "entry_time": r["trigger"]["close_time"], "flat_by": s["flat_by"]}
        out["size"] = size_lots(tools, long_plan, s, real_entry=entry)
        # odds, with trader-timemachine (it knows sells)
        try:
            plan = tm.normalize_plan({"symbol": SYMBOL, "side": side, "date": out["date"], "entry": entry,
                                      "stop": stop, "target": target, "entry_time": r["trigger"]["close_time"],
                                      "setup": "orb", "stop_rule": "auto",
                                      "opening_range": {"minutes": int(s["or_minutes"])}, "interval": "5m"})
            doc_tm = tm.time_machine(json.dumps(doc), plan, now_utc, replays=int(s["replays"]),
                                     until=s["flat_by"] if _on_grid(s["flat_by"]) else None)
            mc = doc_tm["mc"]
            out["odds"] = {"p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
                           "avg_R": mc["avg_R"], "replays": mc["replays"], "sessions": mc["pool_sessions"],
                           "verdict": doc_tm["verdict"], "notes": doc_tm["verdict_notes"],
                           "band_times": mc["band_times"], "bands": mc["bands"],
                           "history": {k: doc_tm["history"].get(k) for k in
                                       ("sessions", "triggered", "target", "stop", "close", "avg_R")}}
        except tm.TMError as e:
            out["odds_error"] = str(e)
        out["watch"] = {"long_plan": long_plan, "mirror": m if side == "sell" else None}
    return out


def _on_grid(hhmm):
    try:
        h, mm = hhmm.split(":")
        return (int(h) * 60 + int(mm)) % 5 == 0
    except ValueError:
        return False


def _reason(side, r, back):
    t = r["trigger"]
    lvl = back(r["level"])
    if side == "buy":
        return "The %s bar closed at %.2f, above the opening-range high %.2f." % (t["bar_start"], t["price"], lvl)
    return "The %s bar closed at %.2f, below the opening-range low %.2f." % (t["bar_start"], back(t["price"]), lvl)


def _merge_history(hist):
    trades = []
    for side, h in hist.items():
        for t in h["trades"]:
            trades.append(dict(t, side=side))
    trades.sort(key=lambda x: (x["date"], x["time"]))
    # one trade a day: the first breakout, either side (as the page trades it)
    first = {}
    for t in trades:
        first.setdefault(t["date"], t)
    day = sorted(first.values(), key=lambda x: x["date"])
    n = len(day)
    return {
        "sessions": hist["buy"]["sessions"], "trades": day[::-1],
        "triggered": n,
        "target": sum(1 for t in day if t["outcome"] == "target"),
        "stop": sum(1 for t in day if t["outcome"] == "stop"),
        "close": sum(1 for t in day if t["outcome"] == "close"),
        "green": sum(1 for t in day if t["R"] > 0),
        "avg_R": round(sum(t["R"] for t in day) / n, 3) if n else None,
        "net_R": round(sum(t["R"] for t in day), 2),
        "by_side": {sd: {k: hist[sd][k] for k in ("triggered", "target", "stop", "close", "avg_R")} for sd in hist},
    }


def size_lots(tools, long_plan, s, real_entry=None):
    """trader-budget's size_trade, in ounces, turned into lots (1 lot = 100 oz).
    Your max loss is balance x risk %. The budget is balance x leverage (a CFD
    account trades on margin); the cash buffer and position caps are off."""
    B = tools.budget
    max_loss = round(float(s["balance"]) * float(s["risk_pct"]) / 100.0, 2)
    cfg = {"max_loss_per_trade_usd": max_loss, "cash_buffer_pct": 0, "max_position_pct": 0,
           "max_order_usd": 0, "min_order_usd": 0, "min_reward_risk": 1.0}
    try:
        # buying power at the real price (a sell is sized on the mirrored chart)
        scale = (long_plan["entry"] / real_entry) if real_entry else 1.0
        z = B.size_trade(long_plan, cfg, budget=float(s["balance"]) * float(s.get("leverage") or 1) * scale)
    except B.SizeError as e:
        return {"lots": 0, "ounces": 0, "refusal": str(e), "max_loss_setting": max_loss}
    return {"ounces": z["qty"], "lots": round(z["qty"] / 100.0, 2), "loss_at_stop": z["max_loss"],
            "gain_at_target": round(z["qty"] * abs(z["target"] - z["entry"]), 2),
            "refusal": z["refusal"], "bound_by": z["bound_by"], "max_loss_setting": max_loss}


# ---------------------------------------------------------------- alerts

def alert_rings(tools, a, price, state, now_utc):
    """trader-alerts' stop, target and flat-by rules for today's plan."""
    if not a.get("watch") or price is None:
        return [], state
    A = tools.alerts
    lp, m = a["watch"]["long_plan"], a["watch"]["mirror"]
    rules = A.rules_from_plan(lp, a["plan"]["flat_by"])
    for r in rules:
        r["id"] = "%s-%s" % (r["id"], lp["date"])
    q = price if m is None else round(m - price, 2)
    rings, state = A.evaluate(rules, {SYMBOL: {"price": q}}, A.market_status(now_utc), state, now_utc)
    plan = a["plan"]
    out = []
    for g in rings:
        if "-plan-stop" in g["id"]:
            out.append(("stop", "Stop hit at %.2f" % plan["stop"],
                        "%s XAUUSD reached your stop. Last %.2f." % (plan["side"].upper(), price)))
        elif "-plan-target" in g["id"]:
            out.append(("target", "Target hit at %.2f" % plan["target"],
                        "%s XAUUSD reached your target (%gR). Last %.2f." % (plan["side"].upper(), plan["target_r"], price)))
        elif "-flat-by" in g["id"]:
            out.append(("flat", "Flat by %s" % plan["flat_by"], "Close anything still open in XAUUSD."))
    return out, state


# --------------------------------------------------------------- kronos

def kronos_forecast(doc_or_text, bars_ahead=12, context=400, sample_count=5, predictor=None):
    """The Kronos model's next `bars_ahead` 5-minute candles from the last
    `context` bars (all hours). Needs torch + pandas (pip install -r requirements.txt)."""
    doc = relabel(doc_or_text) if isinstance(doc_or_text, str) else doc_or_text
    bars = all_bars(doc)[-context:]
    if len(bars) < 100:
        raise DeskError("Kronos needs at least 100 bars, have %d" % len(bars))
    import pandas as pd
    if predictor is None:
        predictor = _kronos_predictor()
    df = pd.DataFrame([{"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]} for b in bars])
    xt = pd.Series(pd.to_datetime([b["t"] for b in bars], unit="s"))
    yt = pd.Series(pd.to_datetime([bars[-1]["t"] + 300 * (k + 1) for k in range(bars_ahead)], unit="s"))
    pred = predictor.predict(df=df, x_timestamp=xt, y_timestamp=yt, pred_len=bars_ahead, T=1.0, top_p=0.9,
                             sample_count=sample_count, verbose=False)
    path = [{"t": bars[-1]["t"] + 300 * (k + 1), "o": round(float(r.open), 2), "h": round(float(r.high), 2),
             "l": round(float(r.low), 2), "c": round(float(r.close), 2)} for k, r in enumerate(pred.itertuples())]
    last = bars[-1]["c"]
    end = path[-1]["c"]
    return {"from_t": bars[-1]["t"], "from": round(last, 2), "path": path, "end": end,
            "change": round(end - last, 2), "direction": "up" if end > last else "down" if end < last else "flat",
            "minutes": bars_ahead * 5}


_PRED = {}


def _kronos_predictor(model="NeoQuasar/Kronos-small", tokenizer="NeoQuasar/Kronos-Tokenizer-base"):
    if "p" not in _PRED:
        root = os.path.dirname(HERE)
        if root not in sys.path:
            sys.path.insert(0, root)
        from model import Kronos, KronosTokenizer, KronosPredictor
        tok = KronosTokenizer.from_pretrained(tokenizer)
        mdl = Kronos.from_pretrained(model)
        _PRED["p"] = KronosPredictor(mdl, tok, max_context=512)
    return _PRED["p"]


# ----------------------------------------------------------------- demo

def demo_text(now_utc=None, days=60, seed=5, start=2400.0):
    """Made-up gold 5-minute bars in Yahoo's chart shape. For looking around only."""
    now_utc = now_utc or utc_now()
    rng = random.Random(seed)
    end = ts_of(now_utc) // 300 * 300
    t = end - days * 86400
    ts, o_, h_, l_, c_, v_ = [], [], [], [], [], []
    p = start
    drift = 0.0
    while t < end:
        d = EPOCH + datetime.timedelta(seconds=t)
        et_h = (d.hour - 4) % 24  # rough ET, fine for made-up data
        wd = d.weekday()
        if wd == 5 or (wd == 6 and d.hour < 22) or (wd == 4 and d.hour >= 21) or et_h == 17:
            t += 300
            continue
        vol = 0.0011 if 8 <= et_h < 12 else 0.0006 if 3 <= et_h < 8 or 12 <= et_h < 16 else 0.0003
        if rng.random() < 0.004:
            drift = rng.gauss(0, 0.00008)
        o = p
        c = o * math.exp(drift + rng.gauss(0, vol))
        h = max(o, c) * math.exp(abs(rng.gauss(0, vol * .5)))
        l = min(o, c) / math.exp(abs(rng.gauss(0, vol * .5)))
        ts.append(t)
        o_.append(round(o, 2)); h_.append(round(h, 2)); l_.append(round(l, 2)); c_.append(round(c, 2))
        v_.append(int(rng.uniform(100, 3000)))
        p = c
        t += 300
    doc = {"chart": {"result": [{"meta": {"symbol": "GC=F", "dataGranularity": "5m", "currency": "USD",
                                          "regularMarketTime": end, "previousClose": c_[-200] if len(c_) > 200 else c_[0]},
                                 "timestamp": ts,
                                 "indicators": {"quote": [{"open": o_, "high": h_, "low": l_, "close": c_,
                                                           "volume": v_}]}}], "error": None}}
    return json.dumps(doc)
