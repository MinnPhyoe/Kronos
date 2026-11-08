"""Phase 10: weekly monitoring and the automatic pause.

    python -m forexapp.monitor report --days 7 [--backtest reports/backtest.json]
    python -m forexapp.monitor autopause

Weekly:
* live expectancy and win rate per pair and setup, against the backtest
* calibration: do 65% signals still win about 65% of the time?
* forecast band hit rate (target about 80% inside the 10-90% band)
* system health: feed disconnects, missed candles, model run time
Auto-pause: if the last 20 outcomes of a pair and setup average below -0.2R,
pause it (the risk check blocks it) and alert.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from .config import Config, UserSettings
from .data.validate import find_gaps

CAL_BINS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]


def calibration(rows: list) -> list:
    out = []
    for lo, hi in CAL_BINS:
        b = [s for s in rows if s.get("kronos_confidence") is not None and lo <= s["kronos_confidence"] < hi
             and s.get("outcome") in ("tp1", "sl")]
        if not b:
            out.append({"bin": "%d-%d%%" % (lo * 100, min(hi, 1) * 100), "n": 0})
            continue
        pred = statistics.mean(s["kronos_confidence"] for s in b)
        act = sum(s["outcome"] == "tp1" for s in b) / len(b)
        out.append({"bin": "%d-%d%%" % (lo * 100, min(hi, 1) * 100), "n": len(b), "predicted": round(pred, 3),
                    "actual": round(act, 3), "gap_points": round(100 * (act - pred), 1)})
    return out


def band_hit_rate(store, pair: str, start: int, end: int) -> dict:
    fs = store.forecasts_between(pair, "5m", start, end)
    candles = {c.open_time: c for c in store.get_candles(pair, "5m", limit=None, start=start, end=end + 86400)}
    inside = total = 0
    for f in fs:
        for b in f.get("band", []):
            c = candles.get(b.get("t"))
            if c is None or "c10" not in b:
                continue
            total += 1
            inside += b["c10"] <= c.close <= b["c90"]
    return {"pair": pair, "points": total, "hit_rate": round(inside / total, 3) if total else None, "target": 0.8}


def weekly_report(store, cfg: Config, days: int = 7, now: float | None = None, backtest: dict | None = None) -> dict:
    now = int(now or time.time())
    start = now - days * 86400
    rows = [s for s in store.get_signals(since=start, limit=5000) if s.get("outcome_r") is not None]
    shown = [s for s in rows if s.get("shown")]
    per = {}
    for s in shown:
        k = "%s:%s" % (s["pair"], s.get("session"))
        per.setdefault(k, []).append(s["outcome_r"])
    live = {k: {"trades": len(v), "expectancy_r": round(statistics.mean(v), 3),
                "win_rate": round(sum(r > 0 for r in v) / len(v), 3)} for k, v in per.items()}
    if backtest:
        for k, v in live.items():
            bt = (backtest.get("by_setup") or {}).get(k)
            if bt:
                v["backtest_expectancy_r"] = bt.get("expectancy_r")
                v["backtest_win_rate"] = bt.get("win_rate")
    runs = [e["detail"]["ms"] for e in store.health_events(start, "run") if e.get("detail")]
    disconnects = len(store.health_events(start, "disconnect"))
    gaps = {}
    for p in cfg.pairs:
        g = find_gaps(store.get_candles(p, "1m", limit=None, start=start, end=now), "1m")
        gaps[p] = g["missing_total"]
    return {
        "period": {"start": start, "end": now, "days": days},
        "live": live,
        "all_shown": {"trades": len(shown), "expectancy_r": round(statistics.mean([s["outcome_r"] for s in shown]), 3)
                      if shown else None},
        "filtered_virtual": {"trades": len(rows) - len(shown),
                             "expectancy_r": round(statistics.mean([s["outcome_r"] for s in rows if not s.get("shown")]), 3)
                             if len(rows) > len(shown) else None},
        "calibration": calibration(shown),
        "band": [band_hit_rate(store, p, start, now) for p in cfg.pairs],
        "health": {"feed_disconnects": disconnects, "missed_1m_candles": gaps,
                   "model_runs": len(runs), "model_ms_mean": round(statistics.mean(runs), 1) if runs else None,
                   "model_ms_p95": round(sorted(runs)[int(0.95 * (len(runs) - 1))], 1) if runs else None},
    }


def auto_pause(store, window: int = 20, threshold: float = -0.2, user_id: str = "default") -> list:
    """Pause pair+session setups whose last ``window`` shown outcomes average below ``threshold`` R."""
    st = UserSettings.from_dict(store.get_settings(user_id))
    rows = [s for s in store.get_signals(status="closed", shown=True, limit=5000) if s.get("outcome_r") is not None]
    by: dict = {}
    for s in rows:                                   # newest first
        by.setdefault("%s:%s" % (s["pair"], s.get("session")), []).append(s["outcome_r"])
    paused = []
    for k, rs in by.items():
        recent = rs[:window]
        if len(recent) >= window and statistics.mean(recent) < threshold and k not in st.paused_setups:
            st.paused_setups.append(k)
            paused.append({"setup": k, "expectancy_r": round(statistics.mean(recent), 3), "trades": len(recent)})
    if paused:
        store.put_settings(st.to_dict(), user_id)
    return paused


def main(argv=None):
    from .data.store import Store
    ap = argparse.ArgumentParser(prog="forexapp.monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--backtest")
    a2 = sub.add_parser("autopause")
    a2.add_argument("--window", type=int, default=20)
    a2.add_argument("--threshold", type=float, default=-0.2)
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    store = Store(cfg.db_url)
    if a.cmd == "report":
        bt = json.load(open(a.backtest)) if a.backtest else None
        print(json.dumps(weekly_report(store, cfg, a.days, backtest=bt), indent=2))
    else:
        print(json.dumps(auto_pause(store, a.window, a.threshold), indent=2))


if __name__ == "__main__":
    main()
