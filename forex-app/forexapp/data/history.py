"""Download or import history, and build every timeframe from 1-minute candles.

    python -m forexapp.data.history download --pairs EURUSD,GBPUSD --years 2
    python -m forexapp.data.history import-histdata EURUSD DAT_ASCII_EURUSD_M1_2024.csv
    python -m forexapp.data.history import-csv EURUSD my_1m.csv
    python -m forexapp.data.history resample --pairs EURUSD
    python -m forexapp.data.history info

You need 2+ years (ideally 5) of 1-minute data. 5m, 15m and 1H are rebuilt
here from the 1-minute candles so every timeframe uses the same rules.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import HIGHER_TIMEFRAMES, Config
from ..timeutil import iso, market_open
from .candles import Candle, resample
from .providers import make_provider
from .store import Store

log = logging.getLogger(__name__)
EST = timezone(timedelta(hours=-5))      # HistData.com files: Eastern Standard Time, no daylight saving


def parse_histdata_line(pair: str, row: list) -> Candle | None:
    """HistData.com generic ASCII M1: 20240102 170000;1.104290;1.104380;1.104290;1.104380;0"""
    if len(row) < 5:
        return None
    dt = datetime.strptime(row[0].strip(), "%Y%m%d %H%M%S").replace(tzinfo=EST)
    t = int(dt.timestamp())
    o, h, l, c = (float(x) for x in row[1:5])
    return Candle(pair, "1m", t, o, h, l, c, int(float(row[5])) if len(row) > 5 and row[5].strip() else 0, 0.0)


def import_histdata(store: Store, pair: str, path: str) -> int:
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f, delimiter=";"):
            c = parse_histdata_line(pair, r)
            if c is not None and market_open(c.open_time):
                rows.append(c)
    store.upsert_candles(rows)
    return len(rows)


def import_csv(store: Store, pair: str, path: str) -> int:
    """CSV with a header: time (ISO or epoch, UTC), open, high, low, close[, volume][, spread]."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            k = {x.lower().strip(): v for x, v in r.items()}
            tv = k.get("time") or k.get("timestamp") or k.get("timestamps") or k.get("datetime")
            try:
                t = int(float(tv))
            except ValueError:
                t = int(datetime.fromisoformat(tv.replace("Z", "+00:00")).replace(
                    tzinfo=timezone.utc if "+" not in tv and "Z" not in tv else None).timestamp())
            rows.append(Candle(pair, "1m", t, float(k["open"]), float(k["high"]), float(k["low"]),
                               float(k["close"]), int(float(k.get("volume") or k.get("tick_volume") or 0)),
                               float(k.get("spread") or 0)))
    store.upsert_candles(rows)
    return len(rows)


def rebuild_higher(store: Store, pair: str, start: int | None = None, end: int | None = None,
                   chunk_days: int = 30) -> dict:
    """Rebuild 5m/15m/1H from stored 1-minute candles, a month at a time."""
    first = store.first_candle_time(pair, "1m")
    last = store.last_candle_time(pair, "1m")
    if first is None:
        return {}
    start = start if start is not None else first
    end = end if end is not None else last + 60
    start -= start % 86400
    counts = {tf: 0 for tf in HIGHER_TIMEFRAMES}
    t = start
    while t < end:
        t2 = min(end, t + chunk_days * 86400)
        base = store.get_candles(pair, "1m", limit=None, start=t, end=t2)
        for tf in HIGHER_TIMEFRAMES:
            hc = resample(base, tf)
            store.upsert_candles(hc)
            counts[tf] += len(hc)
        t = t2
    return counts


async def download(cfg: Config, store: Store, pairs: list, start: int, end: int, chunk_days: int = 7):
    provider = make_provider(cfg)
    for pair in pairs:
        t, total = start, 0
        while t < end:
            t2 = min(end, t + chunk_days * 86400)
            rows = await provider.history(pair, t, t2)
            store.upsert_candles(rows)
            total += len(rows)
            log.info("%s %s .. %s: %d candles (total %d)", pair, iso(t), iso(t2), len(rows), total)
            t = t2
        print("%s: %d 1m candles downloaded" % (pair, total))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.data.history", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--pairs")
    d.add_argument("--years", type=float, default=2.0)
    d.add_argument("--start")
    d.add_argument("--end")
    h = sub.add_parser("import-histdata")
    h.add_argument("pair")
    h.add_argument("files", nargs="+")
    c = sub.add_parser("import-csv")
    c.add_argument("pair")
    c.add_argument("files", nargs="+")
    r = sub.add_parser("resample")
    r.add_argument("--pairs")
    sub.add_parser("info")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    store = Store(cfg.db_url)
    pairs = [p.strip().upper() for p in a.pairs.split(",")] if getattr(a, "pairs", None) else cfg.pairs
    if a.cmd == "download":
        end = int(datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc).timestamp()) if a.end else int(time.time())
        end -= end % 60
        start = (int(datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp()) if a.start
                 else end - int(a.years * 365 * 86400))
        asyncio.run(download(cfg, store, pairs, start, end))
        for p in pairs:
            print(p, rebuild_higher(store, p, start, end))
    elif a.cmd == "import-histdata":
        for f in a.files:
            print("%s: %d candles from %s" % (a.pair, import_histdata(store, a.pair.upper(), f), f))
        print(rebuild_higher(store, a.pair.upper()))
    elif a.cmd == "import-csv":
        for f in a.files:
            print("%s: %d candles from %s" % (a.pair, import_csv(store, a.pair.upper(), f), f))
        print(rebuild_higher(store, a.pair.upper()))
    elif a.cmd == "resample":
        for p in pairs:
            print(p, rebuild_higher(store, p))
    elif a.cmd == "info":
        for p in pairs:
            for tf in ("1m",) + HIGHER_TIMEFRAMES:
                n = store.candle_count(p, tf)
                if n:
                    print("%-7s %-3s %9d candles  %s .. %s" % (p, tf, n, iso(store.first_candle_time(p, tf)),
                                                                iso(store.last_candle_time(p, tf))))


if __name__ == "__main__":
    main()
