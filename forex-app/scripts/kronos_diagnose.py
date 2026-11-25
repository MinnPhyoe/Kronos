"""Does Kronos forecast EUR/USD sensibly? Replay recent history and compare its forecasts with what happened.

    python scripts/kronos_diagnose.py --pair EURUSD --days 12 --cutoffs 10 --paths 20

For each cutoff (a past moment with 2 hours of known future), it forecasts the next 24 five-minute candles
under several input variants, and reports per variant:
  * error of the middle path vs. "no change" (the naive forecast);
  * how often the real price ended inside the 10-90% band;
  * how often the forecast moved toward the average price of its input window ("pull to the mean");
  * the jump between the last real close and the first forecast candle.
"""
from __future__ import annotations

import argparse
import datetime as dt
import lzma
import struct
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
UTC = dt.timezone.utc


def dukascopy_1m(pair: str, days: int) -> pd.DataFrame:
    """Completed days of 1-minute bid candles, with Dukascopy's volume."""
    scale = 1e3 if pair.endswith("JPY") else 1e5
    rows = []
    today = dt.datetime.now(UTC).date()
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        for back in range(days, 0, -1):
            day = today - dt.timedelta(days=back)
            if day.weekday() == 5:
                continue
            url = "https://datafeed.dukascopy.com/datafeed/%s/%04d/%02d/%02d/BID_candles_min_1.bi5" % (
                pair, day.year, day.month - 1, day.day)
            raw = b""
            for attempt in range(3):
                try:
                    r = c.get(url)
                    if r.status_code == 200 and r.content:
                        raw = lzma.decompress(r.content)
                        break
                    print("  %s -> HTTP %d" % (day, r.status_code), flush=True)
                    if r.status_code == 404:
                        break
                except Exception as e:
                    print("  %s -> %s" % (day, str(e)[:80]), flush=True)
                time.sleep(2 + attempt)
            start = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
            for off in range(0, len(raw) - 23, 24):
                t, o, cl, lo, hi, v = struct.unpack(">IIIIIf", raw[off:off + 24])
                if v > 0:
                    rows.append((start + t, o / scale, hi / scale, lo / scale, cl / scale, float(v)))
            print("day %s: %d minutes" % (day, len(raw) // 24), flush=True)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"]).sort_values("time")
    return df


def kraken_5m(pair: str) -> pd.DataFrame:
    """Kraken's public API: the last 720 five-minute candles, with traded volume."""
    r = httpx.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 5}, timeout=30)
    d = r.json()
    key = next(k for k in d["result"] if k != "last")
    rows = [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])) for c in d["result"][key]]
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def to_5m(m: pd.DataFrame) -> pd.DataFrame:
    m = m.copy()
    m["bucket"] = m["time"] - m["time"] % 300
    g = m.groupby("bucket")
    out = pd.DataFrame({"time": g["time"].first() - g["time"].first() % 300, "open": g["open"].first(),
                        "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                        "volume": g["volume"].sum()}).reset_index(drop=True)
    return out


def contiguous(five: pd.DataFrame, i: int, before: int, after: int) -> bool:
    t = five["time"].to_numpy()
    lo, hi = i - before + 1, i + after
    if lo < 0 or hi >= len(t):
        return False
    gaps = np.diff(t[i:hi + 1])                       # the future must be continuous 5-minute candles
    return bool((gaps == 300).all())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--days", type=int, default=12)
    ap.add_argument("--cutoffs", type=int, default=10)
    ap.add_argument("--paths", type=int, default=20)
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--out", default="kronos-diagnosis.md")
    args = ap.parse_args()
    pair, H = args.pair, args.horizon
    pip = 0.01 if pair.endswith("JPY") else 0.0001

    m = dukascopy_1m(pair, args.days)
    source = "Dukascopy"
    if len(m) > 5000:
        five = to_5m(m)
    else:
        five, source = kraken_5m(pair), "Kraken"
        five = five.iloc[:-1]                          # the last one is still forming
    print("5-minute candles from %s: %d" % (source, len(five)), flush=True)

    # cutoffs: spread over the last few trading days, each with 480 candles before and H continuous after
    idx = [i for i in range(480, len(five) - H - 1) if contiguous(five, i, 480, H)]
    if len(idx) < args.cutoffs:
        print("not enough continuous data")
        return 2
    picks = [idx[int(k)] for k in np.linspace(0 if source == "Kraken" else len(idx) * 0.35, len(idx) - 1, args.cutoffs)]

    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer
    tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    tok.eval()
    model.eval()
    pred = KronosPredictor(model, tok, device="cpu", max_context=512)

    variants = {
        "A. as the app feeds it now (480 candles, volume 0)": (480, False),
        "B. 480 candles, real volume": (480, True),
        "C. 240 candles, volume 0": (240, False),
        "D. 240 candles, real volume": (240, True),
    }
    stats = {v: [] for v in variants}
    for n, i in enumerate(picks):
        last = float(five["close"].iloc[i])
        actual_end = float(five["close"].iloc[i + H])
        fut_t = pd.Series(pd.to_datetime(five["time"].iloc[i + 1:i + H + 1].to_numpy(), unit="s"))
        for name, (look, vol) in variants.items():
            w = five.iloc[i - look + 1:i + 1]
            df = w[["open", "high", "low", "close"]].reset_index(drop=True).copy()
            df["volume"] = w["volume"].to_numpy() if vol else 0.0
            df["amount"] = (df["volume"] * df["close"]) if vol else 0.0
            xt = pd.Series(pd.to_datetime(w["time"].to_numpy(), unit="s"))
            torch.manual_seed(1000 + n)
            t0 = time.perf_counter()
            out = pred.predict_batch([df] * args.paths, [xt] * args.paths, [fut_t] * args.paths, pred_len=H,
                                     T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False)
            closes = np.stack([o["close"].to_numpy() for o in out])          # (paths, H)
            mid_end = float(np.median(closes[:, -1]))
            c10, c90 = np.percentile(closes[:, -1], [10, 90])
            first = float(np.median(closes[:, 0]))
            mean_w = float(w["close"].mean())
            stats[name].append({
                "err": abs(mid_end - actual_end) / pip, "naive": abs(last - actual_end) / pip,
                "inside": c10 <= actual_end <= c90, "width": (c90 - c10) / pip,
                "toward_mean": np.sign(mid_end - last) == np.sign(mean_w - last),
                "move": (mid_end - last) / pip, "gap_to_mean": (mean_w - last) / pip,
                "first_jump": abs(first - last) / pip, "up": float((closes[:, -1] > last).mean()),
                "secs": time.perf_counter() - t0})
            s = stats[name][-1]
            print("cutoff %d/%d %s | last %.5f real end %.5f | Kronos middle %.5f (%+.1f pips), band %.5f-%.5f, "
                  "first jump %.1f pips, %.0f s" % (n + 1, len(picks), name[:2], last, actual_end, mid_end, s["move"],
                                                   c10, c90, s["first_jump"], s["secs"]), flush=True)

    L = ["# Kronos-small on %s: forecasts vs. what happened" % pair, "",
         "Prices: %s. %d past moments; %d paths each; forecasting %d five-minute candles (2 hours)."
         % (source, len(picks), args.paths, H), "",
         "| Variant | Error of middle path (pips) | No-change error (pips) | Real price inside 10-90% band | "
         "Band width (pips) | Moved toward window average | First-candle jump (pips) |",
         "|---|---|---|---|---|---|---|"]
    for name, rs in stats.items():
        L.append("| %s | %.1f | %.1f | %d of %d | %.1f | %d of %d | %.1f |" % (
            name, np.mean([r["err"] for r in rs]), np.mean([r["naive"] for r in rs]),
            sum(r["inside"] for r in rs), len(rs), np.mean([r["width"] for r in rs]),
            sum(r["toward_mean"] for r in rs), len(rs), np.mean([r["first_jump"] for r in rs])))
    L += ["", "Pull to the mean: correlation between the forecast move and the distance to the window average:"]
    for name, rs in stats.items():
        a, b = np.array([r["move"] for r in rs]), np.array([r["gap_to_mean"] for r in rs])
        corr = float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else float("nan")
        slope = float(np.polyfit(b, a, 1)[0]) if b.std() > 0 else float("nan")
        L.append("- %s: correlation %.2f, the forecast moves %.0f%% of the way to the average" % (name, corr, 100 * slope))
    report = "\n".join(L)
    Path(args.out).write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
