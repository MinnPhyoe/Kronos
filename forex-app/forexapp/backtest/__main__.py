"""Walk-forward backtest and the three-version comparison.

    python -m forexapp.backtest --pairs EURUSD,GBPUSD --versions rules,base --out reports/
    KRONOS_CHECKPOINT=models/checkpoints/... python -m forexapp.backtest --versions rules,base,finetuned

Versions:
  rules      day-trader rules alone (no forecast)
  base       rules + the base Kronos filter (KRONOS_MODEL)
  finetuned  rules + the fine-tuned Kronos filter (KRONOS_CHECKPOINT)
  bootstrap  rules + the statistical stand-in (for testing the pipeline only)

Kronos earns its place only if version 2 or 3 clearly beats version 1. Test
each pair and session separately. Pass criteria before launch: positive
expectancy in most test windows, not just on average, and calibration that holds.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import time
from pathlib import Path

from ..config import Config
from ..data.store import Store
from ..timeutil import iso, parse_iso
from .engine import candidate_trades
from .metrics import breakdown, pass_criteria, walk_forward

log = logging.getLogger(__name__)


def make_version(name: str, cfg: Config):
    if name == "rules":
        return None
    if name == "bootstrap":
        from ..models.bootstrap import BootstrapForecaster
        return BootstrapForecaster()
    from ..models.kronos_service import KronosForecaster
    c = copy.copy(cfg)
    if name == "base":
        c.kronos_checkpoint = None
        c.kronos_tokenizer_checkpoint = None
    elif name == "finetuned":
        if not cfg.kronos_checkpoint:
            raise SystemExit("finetuned needs KRONOS_CHECKPOINT (and KRONOS_TOKENIZER_CHECKPOINT)")
    else:
        raise SystemExit("unknown version %r" % name)
    return KronosForecaster(c)


def to_markdown(report: dict) -> str:
    L = ["# Backtest report", "", "Generated %s. Test periods only (walk-forward: train %d days, test %d days)."
         % (report["generated"], report["train_days"], report["test_days"]), "",
         "| version | trades | trades/month | expectancy (R) | profit factor | max DD (R) | win rate | positive windows |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v, r in report["versions"].items():
        m = r["test"]
        L.append("| %s | %d | %s | %s | %s | %s | %s | %d of %d |" % (
            v, m["trades"], m["trades_per_month"], m["expectancy_r"], m["profit_factor"], m["max_drawdown_r"],
            m["win_rate"], r["positive_windows"], r["windows_with_trades"]))
    for v, r in report["versions"].items():
        L += ["", "## %s" % v, "", "Pass criteria: " + ", ".join("%s %s" % (k, "yes" if ok else ("n/a" if ok is None else "NO"))
                                                                for k, ok in r["pass"].items()), ""]
        if any(c.get("n") for c in r["calibration"]):
            L += ["| confidence | n | predicted | actual | gap (points) |", "| --- | --- | --- | --- | --- |"]
            for c in r["calibration"]:
                if c.get("n"):
                    L.append("| %s | %d | %s | %s | %s |" % (c["bin"], c["n"], c["predicted"], c["actual"], c["gap_points"]))
            L.append("")
        L += ["| pair:session | trades | expectancy (R) | win rate | profit factor |", "| --- | --- | --- | --- | --- |"]
        for k, m in r["by_setup"].items():
            L.append("| %s | %d | %s | %s | %s |" % (k, m["trades"], m["expectancy_r"], m["win_rate"], m["profit_factor"]))
        if r.get("rejected"):
            L += ["", "Setups skipped by the plan rules: " + ", ".join("%s (%d)" % kv for kv in r["rejected"].items())]
    L += ["", "Spread and slippage are in every fill; entries fill at the next candle's open after the trigger. "
          "Past results on any data are not a promise of future results."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.backtest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs")
    ap.add_argument("--sessions")
    ap.add_argument("--versions", default="rules,base")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--train-days", type=int, default=365)
    ap.add_argument("--test-days", type=int, default=91)
    ap.add_argument("--min-trades", type=int, default=20, help="minimum trades to pick a threshold on a training window")
    ap.add_argument("--no-timemachine", action="store_true")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    store = Store(cfg.db_url)
    pairs = [p.strip().upper() for p in a.pairs.split(",")] if a.pairs else cfg.pairs
    sessions = [s.strip().lower() for s in a.sessions.split(",")] if a.sessions else cfg.sessions
    versions = [v.strip() for v in a.versions.split(",") if v.strip()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"generated": iso(time.time()), "pairs": pairs, "sessions": sessions, "train_days": a.train_days,
              "test_days": a.test_days, "config": {k: v for k, v in cfg.public().items() if k in (
                  "horizon", "paths", "stop_rule", "min_stop_atr", "max_risk_atr", "tp2_cap_r", "slippage_pips",
                  "setup_expiry_minutes", "tm_replays", "tm_lookback_days", "min_confidence")}, "versions": {}}
    data = {}
    for p in pairs:
        start = parse_iso(a.start) if a.start else None
        end = parse_iso(a.end) if a.end else None
        data[p] = store.get_candles(p, "5m", limit=None, start=start, end=end)
        log.info("%s: %d 5m candles %s .. %s", p, len(data[p]), iso(data[p][0].open_time) if data[p] else "-",
                 iso(data[p][-1].open_time) if data[p] else "-")
    span = [c.open_time for p in pairs for c in (data[p][:1] + data[p][-1:])]
    if not span:
        raise SystemExit("no 5m candles stored; download history first (python -m forexapp.data.history)")
    t0, t1 = min(span), max(span) + 300
    for v in versions:
        fc = make_version(v, cfg)
        trades, rejected = [], {}
        for p in pairs:
            tr, rej = candidate_trades(data[p], p, cfg, sessions, forecaster=fc, cache=store,
                                       use_tm=not a.no_timemachine,
                                       progress=lambda pair, i, n: log.info("%s %s: day %d of %d", v, pair, i, n))
            trades += tr
            for k, n in rej.items():
                rejected[k] = rejected.get(k, 0) + n
        wf = walk_forward(trades, t0, t1, a.train_days, a.test_days, use_filter=fc is not None,
                          use_tm=not a.no_timemachine, min_trades=a.min_trades, default_threshold=cfg.min_confidence)
        tested = [t for t in trades if any(t.trigger_time == x["trigger_time"] and t.pair == x["pair"] for x in wf["trades"])]
        wf["by_setup"] = breakdown(tested)
        wf["pass"] = pass_criteria(wf)
        wf["rejected"] = rejected
        wf["candidates"] = len(trades)
        report["versions"][v] = wf
        log.info("%s: %s", v, wf["test"])
    (out / "backtest.json").write_text(json.dumps(report, indent=1, default=str))
    (out / "backtest.md").write_text(to_markdown(report))
    print(to_markdown(report))
    print("Wrote %s and %s" % (out / "backtest.json", out / "backtest.md"))


if __name__ == "__main__":
    main()
