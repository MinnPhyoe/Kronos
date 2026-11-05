"""OBSIDIAN Gold: where the candles come from.

  yahoo    Yahoo Finance chart endpoint, 15-minute bars for 60 days.
           GC=F (COMEX gold futures, default) or XAUUSD=X (spot). Free and
           unofficial: it can lag a little or rate-limit.
  binance  Binance PAXGUSDT, a token backed by one ounce of gold. Trades
           24/7, follows spot gold closely but not exactly.
  csv      A file you export from MT4/MT5 (History Center) or TradingView.
  demo     Made-up candles, only so the page has something to draw.

All return M15 candles as dicts: {"t", "o", "h", "l", "c", "v"} (t = epoch
seconds UTC, candle open time), oldest first.
"""

import csv
import io
import json
import math
import random
import time
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (OBSIDIAN Gold; personal use)"


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# ----------------------------------------------------------------- yahoo

def yahoo(symbol="GC=F", days=60):
    last_err = None
    for host in ("query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%dd&interval=15m&includePrePost=true"
               % (host, urllib.request.quote(symbol), days))
        try:
            return parse_yahoo(_get(url))
        except Exception as e:  # try the other host
            last_err = e
    raise RuntimeError("Yahoo: %s" % last_err)


def parse_yahoo(text):
    doc = json.loads(text)
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        err = (doc.get("chart") or {}).get("error")
        raise ValueError("no chart result (%s)" % (err or "empty"))
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None])[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        t = int(t) - int(t) % 900
        out.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)})
    return dedupe(out)


# --------------------------------------------------------------- binance

def binance(symbol="PAXGUSDT", days=60):
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out = []
    while start < end:
        url = ("https://api.binance.com/api/v3/klines?symbol=%s&interval=15m&startTime=%d&limit=1000"
               % (symbol, start))
        rows = json.loads(_get(url))
        if not rows:
            break
        out += parse_binance(rows)
        start = rows[-1][0] + 900 * 1000
        if len(rows) < 1000:
            break
    return dedupe(out)


def parse_binance(rows):
    return [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": float(r[5])} for r in rows]


# ------------------------------------------------------------------- csv

def parse_csv(text, utc_offset_hours=0.0):
    """MT4/MT5 exports (<DATE> <TIME> <OPEN> ..., tab or comma), TradingView
    exports (time,open,high,low,close), or any file with date/time and
    open/high/low/close columns. `utc_offset_hours` is your broker's server
    time offset (many MT brokers run at UTC+2 or UTC+3)."""
    text = text.lstrip("﻿")
    sample = text[:2000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(x.strip() for x in r)]
    if not rows:
        raise ValueError("The file is empty.")
    head = [h.strip().strip("<>").lower() for h in rows[0]]
    has_head = any(h in head for h in ("open", "close", "date", "time", "timestamp", "timestamps"))
    if has_head:
        body = rows[1:]
    else:  # MT4 history without header: date,time,o,h,l,c,v
        head = ["date", "time", "open", "high", "low", "close", "volume"][:len(rows[0])]
        body = rows

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    ci = {k: col(*v) for k, v in {
        "date": ("date", "day"), "time": ("time", "timestamp", "timestamps", "datetime", "date_time"),
        "o": ("open", "o"), "h": ("high", "h"), "l": ("low", "l"), "c": ("close", "c"),
        "v": ("tickvol", "tick_volume", "volume", "vol", "v")}.items()}
    if None in (ci["o"], ci["h"], ci["l"], ci["c"]) or (ci["date"] is None and ci["time"] is None):
        raise ValueError("Could not find date/time and open, high, low, close columns. Found: %s" % ", ".join(head))
    out = []
    shift = int(utc_offset_hours * 3600)
    for r in body:
        try:
            if ci["date"] is not None and ci["time"] is not None:
                t = _ts(r[ci["date"]].strip() + " " + r[ci["time"]].strip())
            else:
                t = _ts(r[ci["time"] if ci["time"] is not None else ci["date"]].strip())
            o, h, l, c = (float(r[ci[k]]) for k in ("o", "h", "l", "c"))
            v = float(r[ci["v"]]) if ci["v"] is not None and r[ci["v"]].strip() else 0.0
        except (ValueError, IndexError):
            continue
        out.append({"t": t - shift, "o": o, "h": h, "l": l, "c": c, "v": v})
    if not out:
        raise ValueError("No candle rows could be read.")
    out = dedupe(out)
    step = _step(out)
    return out, step


def _ts(s):
    s = s.strip().replace("T", " ").rstrip("Z")
    if s.replace(".", "").isdigit():
        n = float(s)
        return int(n / 1000) if n > 1e11 else int(n)
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+")[0]
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M",
                "%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    raise ValueError("bad time %r" % s)


def _step(cs):
    gaps = sorted(b["t"] - a["t"] for a, b in zip(cs, cs[1:]) if b["t"] > a["t"])
    return gaps[len(gaps) // 2] if gaps else 0


def dedupe(cs):
    by = {}
    for c in cs:
        by[c["t"]] = c
    return [by[t] for t in sorted(by)]


def to_m15(cs, step):
    """Bring a CSV to M15. Smaller bars are grouped; M15 passes through.
    Bigger bars cannot be split, so they are refused."""
    from engine import resample
    if step and step > 900:
        raise ValueError("This file has %d-minute candles. Export M1, M5 or M15 so the "
                         "app can build every timeframe from it." % (step // 60))
    return cs if step == 900 else resample(cs, 900)


# ------------------------------------------------------------------ demo

def demo(days=60, seed=3, start_price=2400.0, now=None):
    """Made-up gold-like candles: a random walk with session-shaped volatility.
    For drawing the page only. Not real prices."""
    rng = random.Random(seed)
    now = now or int(time.time())
    end = now - now % 900
    t = end - days * 86400
    p, out, drift = start_price, [], 0.0
    while t < end:
        d = datetime.fromtimestamp(t, tz=timezone.utc)
        if d.weekday() == 5 or (d.weekday() == 4 and d.hour >= 21) or (d.weekday() == 6 and d.hour < 22):
            t += 900
            continue
        hr = d.hour
        vol = 0.0009 if 7 <= hr < 10 or 12 <= hr < 16 else 0.0004
        if rng.random() < 0.01:
            drift = rng.gauss(0, 0.00012)
        o = p
        c = o * math.exp(drift + rng.gauss(0, vol))
        h = max(o, c) * math.exp(abs(rng.gauss(0, vol * 0.6)))
        l = min(o, c) / math.exp(abs(rng.gauss(0, vol * 0.6)))
        out.append({"t": t, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
                    "v": float(int(rng.uniform(200, 2000)))})
        p = c
        t += 900
    return out
