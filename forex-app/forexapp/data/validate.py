"""Check the candles before moving on.

    python -m forexapp.data.validate gaps --pair EURUSD --tf 1m --days 30
    python -m forexapp.data.validate compare --pair EURUSD --days 7 --against broker.csv
    python -m forexapp.data.validate compare --pair EURUSD --days 7 --against oanda

``gaps`` lists missing candles during market hours (weekend gaps are expected
and only counted). ``compare`` checks a week of your candles against your
broker's: prices should match within a pip or two.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import time
from datetime import datetime, timezone

from .. import instruments
from ..config import TIMEFRAMES, Config, norm_tf
from ..timeutil import is_weekend_gap, iso
from .candles import Candle
from .store import Store


def find_gaps(candles: list, tf: str) -> dict:
    """Missing candles between consecutive candles. Weekend closes are reported
    separately: they are expected and never filled."""
    secs = TIMEFRAMES[tf]
    gaps, weekends = [], 0
    for a, b in zip(candles, candles[1:]):
        if b.open_time - a.open_time <= secs:
            continue
        if is_weekend_gap(a.open_time, b.open_time, secs):
            weekends += 1
            continue
        gaps.append({"after": iso(a.open_time), "before": iso(b.open_time),
                     "missing": (b.open_time - a.open_time) // secs - 1})
    return {"candles": len(candles), "weekend_gaps": weekends, "gaps": gaps,
            "missing_total": sum(g["missing"] for g in gaps)}


def compare(mine: list, theirs: list, pair: str) -> dict:
    ins = instruments.get(pair)
    by_t = {c.open_time: c for c in theirs}
    diffs = []
    for c in mine:
        o = by_t.get(c.open_time)
        if o is None:
            continue
        for a, b in ((c.open, o.open), (c.high, o.high), (c.low, o.low), (c.close, o.close)):
            diffs.append(abs(a - b) / ins.pip)
    if not diffs:
        return {"matched": 0, "note": "no common candles"}
    diffs.sort()
    return {"matched": len(diffs) // 4, "mine": len(mine), "theirs": len(theirs),
            "mean_pips": round(statistics.mean(diffs), 3), "p95_pips": round(diffs[int(0.95 * (len(diffs) - 1))], 3),
            "max_pips": round(diffs[-1], 3), "ok": diffs[int(0.95 * (len(diffs) - 1))] <= 2.0}


def read_broker_csv(path: str, pair: str, tf: str) -> list:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            k = {x.lower().strip(): v for x, v in r.items()}
            tv = k.get("time") or k.get("timestamp") or k.get("datetime")
            try:
                t = int(float(tv))
            except ValueError:
                t = int(datetime.fromisoformat(tv.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())
            out.append(Candle(pair, tf, t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.data.validate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gaps")
    g.add_argument("--pair", required=True)
    g.add_argument("--tf", default="1m")
    g.add_argument("--days", type=float, default=30)
    c = sub.add_parser("compare")
    c.add_argument("--pair", required=True)
    c.add_argument("--tf", default="1m")
    c.add_argument("--days", type=float, default=7)
    c.add_argument("--against", required=True, help="a broker CSV, or 'oanda' to fetch OANDA candles")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    store = Store(cfg.db_url)
    pair, tf = a.pair.upper(), norm_tf(a.tf)
    end = store.last_candle_time(pair, tf) or int(time.time())
    start = end - int(a.days * 86400)
    mine = store.get_candles(pair, tf, limit=None, start=start, end=end + 1)
    if a.cmd == "gaps":
        r = find_gaps(mine, tf)
        print("%s %s: %d candles, %d weekend gaps, %d gaps with %d missing candles"
              % (pair, tf, r["candles"], r["weekend_gaps"], len(r["gaps"]), r["missing_total"]))
        for x in r["gaps"][:50]:
            print("  missing %d after %s" % (x["missing"], x["after"]))
        return
    if a.against == "oanda":
        from .providers.oanda import OandaProvider
        if tf != "1m":
            raise SystemExit("compare against OANDA uses 1m candles")
        theirs = asyncio.run(OandaProvider(cfg.oanda_token, cfg.oanda_account, cfg.oanda_env,
                                           cfg.price_basis).history(pair, start, end + 60))
    else:
        theirs = read_broker_csv(a.against, pair, tf)
    r = compare(mine, theirs, pair)
    print(r)
    if r.get("ok") is False:
        print("More than 2 pips apart on the 95th percentile: check the price basis (bid vs mid) and time zone.")


if __name__ == "__main__":
    main()
