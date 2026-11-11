"""Analyse one pair for today with the engine's own code, and write a short report.

    python scripts/analyse_today.py --pair EURUSD --out report.md --json report.json

It fetches 5-minute candles (Yahoo first, then Dukascopy's free price feed), this week's economic
calendar, and then asks the engine, in-process, for:
  * the Day Trader opening ranges today (London and New York) and whether they broke out;
  * the Kronos outlook on 5- and 15-minute candles;
  * the full check (Day Trader risk check, Kronos, the Time Machine) now, and at each breakout as it
    happened, using only the candles up to that moment.
Analysis only: it never places a trade. Kronos and Time Machine numbers are estimates, not win rates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import lzma
import os
import struct
import sys
import time
from pathlib import Path

import httpx

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
YAHOO = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X", "AUDUSD": "AUDUSD=X", "XAUUSD": "GC=F"}
DUKA_SCALE = {"USDJPY": 1e3, "XAUUSD": 1e3}
UTC = dt.timezone.utc


# ---------------------------------------------------------------- prices
def yahoo_5m(pair: str, client: httpx.Client) -> list[dict]:
    sym = YAHOO.get(pair, pair + "=X")
    last = None
    for attempt in range(3):
        for host in ("query1", "query2"):
            try:
                r = client.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                               params={"interval": "5m", "range": "60d", "includePrePost": "false"})
            except httpx.HTTPError as e:
                last = str(e)
                continue
            if r.status_code != 200:
                last = "HTTP %d" % r.status_code
                continue
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = []
            for i, t in enumerate(res.get("timestamp") or []):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                rows.append({"time": int(t), "open": o, "high": h, "low": l, "close": c})
            if rows:
                return rows
            last = "no rows"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("Yahoo: %s" % last)


def _duka_get(client: httpx.Client, url: str) -> bytes:
    r = client.get(url)
    if r.status_code != 200 or not r.content:
        return b""
    try:
        return lzma.decompress(r.content)
    except lzma.LZMAError:
        return b""


def dukascopy_5m(pair: str, client: httpx.Client, days: int = 60) -> list[dict]:
    """Dukascopy: one file of 1-minute candles per past day, tick files per hour for today (and for
    yesterday if its day file is not published yet). Months are zero-based in the URL."""
    scale = DUKA_SCALE.get(pair, 1e5)
    base = "https://datafeed.dukascopy.com/datafeed/%s/%04d/%02d/%02d/"
    now = dt.datetime.now(UTC)
    minutes: dict[int, list] = {}
    for back in range(days, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        if day.weekday() == 5:                       # Saturday: closed
            continue
        start = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        url = base % (pair, day.year, day.month - 1, day.day)
        raw = b"" if back == 0 else _duka_get(client, url + "BID_candles_min_1.bi5")
        if raw:
            for off in range(0, len(raw) - 23, 24):
                t, a, b, c, d, v = struct.unpack(">IIIIIf", raw[off:off + 24])
                o, cl, lo, hi = a / scale, b / scale, c / scale, d / scale   # time, open, close, low, high, volume
                if not (lo <= min(o, cl) and max(o, cl) <= hi):
                    o, hi, lo, cl = a / scale, b / scale, c / scale, d / scale  # other field order, just in case
                if v > 0:
                    minutes[start + t] = [o, hi, lo, cl]
            continue
        last_hour = now.hour if back == 0 else 23
        for h in range(0, last_hour + 1):
            ticks = _duka_get(client, url + "%02dh_ticks.bi5" % h)
            for off in range(0, len(ticks) - 19, 20):
                ms, ask, bid, av, bv = struct.unpack(">IIIff", ticks[off:off + 20])
                t = start + h * 3600 + ms // 1000
                m = t - t % 60
                p = bid / scale
                bar = minutes.get(m)
                if bar is None:
                    minutes[m] = [p, p, p, p]
                else:
                    bar[1], bar[2], bar[3] = max(bar[1], p), min(bar[2], p), p
    if not minutes:
        raise RuntimeError("Dukascopy: no data")
    return to_5m([{"time": t, "open": b[0], "high": b[1], "low": b[2], "close": b[3]} for t, b in sorted(minutes.items())])


def to_5m(rows: list[dict], step: int = 300) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        t = r["time"] - r["time"] % step
        if out and out[-1]["time"] == t:
            b = out[-1]
            b["high"], b["low"], b["close"] = max(b["high"], r["high"]), min(b["low"], r["low"]), r["close"]
        else:
            out.append({"time": t, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]})
    return out


def calendar(client: httpx.Client) -> list[dict]:
    try:
        r = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


# ---------------------------------------------------------------- the report
def fmt(x, d=5):
    return "–" if x is None else ("%.*f" % (d, x))


def pct(x):
    return "–" if x is None else "%.0f%%" % (100 * x)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--json", default="report.json")
    ap.add_argument("--candles-file", help="offline: a JSON list of 5-minute candles instead of fetching")
    ap.add_argument("--balance", type=float, default=10_000.0)
    ap.add_argument("--currency", default="USD")
    args = ap.parse_args()
    pair = args.pair.upper().replace("/", "")

    from fastapi.testclient import TestClient
    from forexapp.config import Config
    from forexapp.engine_server import create_engine_app

    source, errors = None, []
    with httpx.Client(timeout=30, headers={"User-Agent": UA, "Accept": "*/*"}, follow_redirects=True) as client:
        if args.candles_file:
            five, source = json.loads(Path(args.candles_file).read_text()), "file"
        else:
            five = []
            for name, fn in (("Yahoo Finance", yahoo_5m), ("Dukascopy", dukascopy_5m)):
                try:
                    five, source = fn(pair, client), name
                    break
                except Exception as e:
                    errors.append("%s: %s" % (name, e))
            if not five:
                print("Could not get prices:", "; ".join(errors))
                return 2
        events = [] if args.candles_file else calendar(client)

    five = [c for c in five if c["high"] >= c["low"] > 0]
    five.sort(key=lambda c: c["time"])
    # drop the candle that is still forming
    now_ts = time.time() if not args.candles_file else five[-1]["time"] + 300
    if five[-1]["time"] + 300 > now_ts:
        five = five[:-1]
    last = five[-1]
    asof = dt.datetime.fromtimestamp(last["time"] + 300, UTC)
    day0 = int(dt.datetime(asof.year, asof.month, asof.day, tzinfo=UTC).timestamp())
    today = [c for c in five if c["time"] >= day0]
    ystart = day0 - 86400
    while ystart > five[0]["time"] and not [c for c in five if ystart <= c["time"] < ystart + 86400]:
        ystart -= 86400                              # skip the weekend
    yday = [c for c in five if ystart <= c["time"] < ystart + 86400]

    # daily ranges (UTC days) for an average daily range
    days: dict[int, list] = {}
    for c in five:
        days.setdefault(c["time"] - c["time"] % 86400, []).append(c)
    full = [v for k, v in sorted(days.items()) if k < day0 and len(v) > 200]
    adr = sum(max(c["high"] for c in v) - min(c["low"] for c in v) for v in full[-14:]) / max(1, len(full[-14:]))
    asia = [c for c in today if c["time"] < day0 + 7 * 3600]

    cfg = Config.from_env()
    engine = TestClient(create_engine_app(cfg, token=""))

    def post(path, body):
        r = engine.post(path, json=body)
        if r.status_code != 200:
            return {"error": "%s %s" % (r.status_code, r.text[:300])}
        return r.json()

    fifteen = to_5m(five, 900)
    outlook = {"5m": post("/forecast", {"pair": pair, "timeframe": "M5", "candles": five[-480:], "horizon": 24}),
               "15m": post("/forecast", {"pair": pair, "timeframe": "M15", "candles": fifteen[-480:], "horizon": 24})}
    orb = post("/orb", {"pair": pair, "candles": today if len(today) > 3 else five[-300:]})
    account = {"balance": args.balance, "currency": args.currency, "risk_per_trade_pct": 0.5, "daily_loss_pct": 2.0,
               "max_trades_per_day": 3, "min_reward_risk": 1.0}

    def best_at(cut: int | None):
        cs = [c for c in five if cut is None or c["time"] + 300 <= cut]
        return post("/best", {"markets": [{"pair": pair, "timeframe": "M5", "orb_candles": cs}],
                              "account": account, "events": events, "at": cut})

    checks = {"now": best_at(None)}
    for s in orb.get("setups", []):
        es = s.get("entry_state") or {}
        tt = es.get("trigger_time")
        if s.get("status") in ("triggered", "expired", "closed") and tt:
            checks["at the %s breakout" % s["label"]] = best_at(int(tt) + 300)

    # ------------------------------------------------------------ write it up
    d = 3 if pair.endswith("JPY") else (2 if pair == "XAUUSD" else 5)
    pip = 0.01 if pair.endswith("JPY") else (0.1 if pair == "XAUUSD" else 0.0001)
    L = []
    disp = pair[:3] + "/" + pair[3:]
    L.append("# %s today: %s, as of %s UTC" % (disp, asof.strftime("%a %d %b %Y"), asof.strftime("%H:%M")))
    L.append("")
    L.append("Prices: %s, 5-minute candles. Analysis only; Kronos and Time Machine numbers are estimates, not win rates." % source)
    if errors:
        L.append("(%s)" % "; ".join(errors))
    L.append("")
    L.append("## Where price is")
    if today:
        hi, lo = max(c["high"] for c in today), min(c["low"] for c in today)
        L.append("- Now **%s**. Today since 00:00 UTC: opened %s, high %s, low %s (range %.1f pips; average day %.1f pips)."
                 % (fmt(last["close"], d), fmt(today[0]["open"], d), fmt(hi, d), fmt(lo, d), (hi - lo) / pip, adr / pip))
    if yday:
        L.append("- Previous day: high %s, low %s, close %s." % (fmt(max(c["high"] for c in yday), d),
                                                                fmt(min(c["low"] for c in yday), d), fmt(yday[-1]["close"], d)))
    if asia:
        L.append("- Asian session (00:00–07:00 UTC): %s – %s." % (fmt(min(c["low"] for c in asia), d), fmt(max(c["high"] for c in asia), d)))
    L.append("")
    L.append("## Day Trader opening ranges")
    for s in orb.get("setups", []):
        rng, es = s.get("range") or {}, s.get("entry_state") or {}
        line = "- **%s**: %s" % (s["label"], s.get("status"))
        if rng:
            line += ". Range %s – %s (%.1f pips)" % (fmt(rng.get("low"), d), fmt(rng.get("high"), d), rng.get("height_pips") or 0)
        if es.get("side"):
            t = dt.datetime.fromtimestamp(es["trigger_time"], UTC).strftime("%H:%M") if es.get("trigger_time") else "?"
            line += "; a 5-minute candle closed %s it at %s UTC (%s)" % ("above" if es["side"] == "buy" else "below", t, fmt(es.get("price"), d))
        L.append(line + ".")
    if "error" in orb:
        L.append("- (opening ranges: %s)" % orb["error"])
    L.append("")
    L.append("## Kronos outlook")
    for tf, f in outlook.items():
        if "error" in f:
            L.append("- %s: %s" % (tf, f["error"]))
            continue
        b = f["band"][-1]
        hours = 24 * (5 if tf == "5m" else 15) / 60
        L.append("- **%s candles, next %g hours**: %s of %d paths end higher. In %g hours: middle %s, 10–90%% band %s – %s. Model: %s."
                 % (tf, hours, pct(f["direction_prob"]), f["n_paths"], hours, fmt(b["p50"], d), fmt(b["c10"], d), fmt(b["c90"], d), f["model"]))
    L.append("")
    L.append("## The full check: Day Trader risk check, Kronos, the Time Machine")
    L.append("Your second risk engine (FX Desk's check against your own records) runs in the Mac app, so it is not part of this report.")

    def session_of(name: str) -> str:
        return "New York" if name.startswith("New York") else name.split()[0]

    for when, r in checks.items():
        if "error" in r:
            L.append("- %s: %s" % (when, r["error"]))
            continue
        lines, passed = [], []
        for c in r.get("ranked", []):
            label = session_of(c["name"])
            if when != "now" and not when.startswith("at the " + label):
                continue                              # at a breakout, show only that session's setup
            reasons = [w for w in c["why_not"] if not w.startswith("OBSIDIAN")]
            if when == "now" and any("outside the session's entry window" in w for w in reasons):
                lines.append("  - %s %s: its entry window has closed." % (c["side"].upper(), c["name"]))
                continue
            k, tmc = c["checks"]["kronos"], c["checks"]["timemachine"]
            ok = not reasons and (c.get("score_R") or 0) > 0
            if ok:
                passed.append(c)
            lines.append("  - %s %s: entry %s, stop %s, target %s. Kronos: %s of paths reach the target first (average %s R). "
                         "Time Machine: %s target first (break-even %s). %s"
                         % (c["side"].upper(), c["name"], fmt(c.get("entry"), d), fmt(c.get("stop"), d), fmt(c.get("target"), d),
                            pct(k.get("confidence")), "–" if k.get("avg_R") is None else "%+.2f" % k["avg_R"],
                            pct(tmc.get("odds")), pct(tmc.get("break_even")),
                            ("**Passed every check here** (cautious estimate %+.2f R)." % c["score_R"]) if ok
                            else "Not taken: " + "; ".join(reasons or ["the cautious estimate is not above zero"])))
        head = ("%d setup passed" % len(passed) if len(passed) == 1 else "%d setups passed" % len(passed)) if passed \
            else ("nothing passed" if lines else "no setup to check")
        L.append("- **%s**: %s." % (when[0].upper() + when[1:], head))
        L.extend(lines)
    todays = [e for e in events if str(e.get("date", "")).startswith(asof.strftime("%Y-%m-%d")) and e.get("country") in ("USD", "EUR")]
    L.append("## News today (USD and EUR)")
    if todays:
        for e in todays:
            L.append("- %s %s %s (%s impact)" % (str(e.get("date", ""))[11:16], e.get("country"), e.get("title"), e.get("impact")))
    else:
        L.append("- %s" % ("Nothing listed for USD or EUR today." if events else "The calendar could not be fetched."))
    L.append("")
    L.append("_Account used for sizing: %s %s, 0.5%% risk a trade. The app never places a trade._" % (f"{args.balance:,.0f}", args.currency))

    report = "\n".join(L)
    Path(args.out).write_text(report)
    Path(args.json).write_text(json.dumps({"source": source, "asof": asof.isoformat(), "orb": orb, "outlook": {
        k: {x: v.get(x) for x in ("direction_prob", "n_paths", "model", "band", "error")} for k, v in outlook.items()},
        "checks": checks, "events_today": todays}, indent=1, default=str))
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
