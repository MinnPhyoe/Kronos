"""Forecast checks for Phase 2 (base model) and Phase 6 (fine-tuned vs base).

    python -m forexapp.models.evaluate --pair EURUSD --tf 5m --days 90 --samples 300
    KRONOS_CHECKPOINT=models/checkpoints/forex-small-2026-11 python -m forexapp.models.evaluate ...

For random points in the period (only candles closed by then go in):

* band hit rate: how often the real close lands inside the 10-90% band of
  the forecast closes. Aim for about 80%. Much less means the band is too
  narrow; note it for fine-tuning.
* range band hit rate: real low and high inside p10 of lows / p90 of highs.
* error on close (pips) of the median path, and
* direction accuracy: share of times the median path got the direction right.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics

import numpy as np

from .. import instruments
from ..config import TIMEFRAMES, Config, norm_tf
from ..data.store import Store
from .kronos_service import make_forecaster


def evaluate(store: Store, forecaster, pair: str, tf: str, start: int, end: int, samples: int = 200,
             lookback: int = 480, horizon: int = 24, n_paths: int = 30, seed: int = 1) -> dict:
    ins = instruments.get(pair)
    allc = store.get_candles(pair, tf, limit=None, start=start - lookback * TIMEFRAMES[tf] * 3, end=end)
    idx = [i for i, c in enumerate(allc) if c.open_time >= start and lookback <= i < len(allc) - horizon]
    rng = random.Random(seed)
    picks = sorted(rng.sample(idx, min(samples, len(idx))))
    inside = inside_rng = total = 0
    errs, dir_ok = [], 0
    for i in picks:
        hist = allc[i - lookback + 1:i + 1]
        actual = allc[i + 1:i + 1 + horizon]
        if len(actual) < horizon:
            continue
        f = forecaster.forecast(hist, pair, tf, horizon, n_paths)
        band = f.band()
        for b, a in zip(band, actual):
            total += 1
            inside += b["c10"] <= a.close <= b["c90"]
            inside_rng += (a.low >= b["p10"]) and (a.high <= b["p90"])
        med = f.median_path()
        errs.append(abs(med[-1] - actual[-1].close) / ins.pip)
        dir_ok += np.sign(med[-1] - hist[-1].close) == np.sign(actual[-1].close - hist[-1].close)
    n = len(errs)
    return {"pair": pair, "tf": tf, "model": getattr(forecaster, "name", "?"), "forecasts": n,
            "band_hit_rate": round(inside / total, 4) if total else None,
            "range_band_hit_rate": round(inside_rng / total, 4) if total else None,
            "median_close_error_pips": round(statistics.mean(errs), 2) if errs else None,
            "direction_accuracy": round(dir_ok / n, 4) if n else None,
            "target_band_hit_rate": 0.8}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.models.evaluate")
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--days", type=float, default=90)
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    store = Store(cfg.db_url)
    tf = norm_tf(a.tf)
    pair = a.pair.upper()
    end = store.last_candle_time(pair, tf)
    if end is None:
        raise SystemExit("no %s %s candles stored; run forexapp.data.history first" % (pair, tf))
    r = evaluate(store, make_forecaster(cfg), pair, tf, end - int(a.days * 86400), end + 1, a.samples,
                 cfg.lookback, cfg.horizon, cfg.paths)
    print(json.dumps(r, indent=2))
    if r["band_hit_rate"] is not None and r["band_hit_rate"] < 0.7:
        print("The band is too narrow (real closes land inside it less than 70% of the time): note it for fine-tuning.")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(r, f, indent=2)


if __name__ == "__main__":
    main()
