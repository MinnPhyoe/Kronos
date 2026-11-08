"""Backtest metrics and walk-forward windows.

| metric          | what it means                          | aim for |
| --------------- | -------------------------------------- | ------- |
| expectancy (R)  | average profit per trade, in risk units | above +0.1R after costs |
| profit factor   | gross wins / gross losses              | above 1.3 |
| max drawdown    | largest peak-to-trough drop (R)        | what a trader would tolerate (e.g. under 15R) |
| win rate        | share of winning trades                | report it, don't optimise for it alone |
| trades / month  | how often signals appear               | enough to matter (e.g. 20+) |
| calibration     | do 65% signals win about 65%?          | within about 5 points |
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine import Trade, passes

THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70)
CAL_BINS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def metrics(trades: list, months: float | None = None) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "expectancy_r": None, "profit_factor": None, "max_drawdown_r": 0.0, "win_rate": None,
                "trades_per_month": 0.0, "total_r": 0.0}
    rs = [t.R for t in sorted(trades, key=lambda t: t.trigger_time)]
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    if months is None:
        span = (max(t.trigger_time for t in trades) - min(t.trigger_time for t in trades)) / (30.44 * 86400)
        months = max(span, 1.0)
    return {"trades": n, "expectancy_r": round(sum(rs) / n, 4), "profit_factor": round(wins / losses, 3) if losses else None,
            "max_drawdown_r": round(dd, 2), "win_rate": round(sum(r > 0 for r in rs) / n, 4),
            "trades_per_month": round(n / months, 1), "total_r": round(sum(rs), 2)}


def calibration(trades: list) -> list:
    out = []
    for lo, hi in CAL_BINS:
        b = [t for t in trades if t.confidence is not None and lo <= t.confidence < hi and t.outcome in ("tp1", "sl")]
        if not b:
            out.append({"bin": "%d-%d%%" % (100 * lo, min(100, 100 * hi)), "n": 0})
            continue
        pred = sum(t.confidence for t in b) / len(b)
        act = sum(t.outcome == "tp1" for t in b) / len(b)
        out.append({"bin": "%d-%d%%" % (100 * lo, min(100, 100 * hi)), "n": len(b), "predicted": round(pred, 3),
                    "actual": round(act, 3), "gap_points": round(100 * (act - pred), 1)})
    return out


def _ts(d: datetime) -> int:
    return int(d.timestamp())


def windows(start: int, end: int, train_days: int, test_days: int) -> list:
    """Rolling (train_start, train_end, test_end): train on train_days, test on the next test_days,
    move forward by test_days until today."""
    out = []
    t = datetime.fromtimestamp(start, timezone.utc)
    last = datetime.fromtimestamp(end, timezone.utc)
    while True:
        tr_end = t + timedelta(days=train_days)
        te_end = tr_end + timedelta(days=test_days)
        if tr_end >= last:
            break
        out.append((_ts(t), _ts(tr_end), _ts(min(te_end, last))))
        if te_end >= last:
            break
        t = t + timedelta(days=test_days)
    return out


def choose_threshold(train: list, use_tm: bool, min_trades: int, default: float) -> float:
    """Pick the confidence threshold on the TRAINING window only (never on test results)."""
    best, best_e = default, None
    for th in THRESHOLDS:
        sel = [t for t in train if passes(t, th, use_tm=use_tm)]
        if len(sel) < min_trades:
            continue
        e = sum(t.R for t in sel) / len(sel)
        if best_e is None or e > best_e:
            best, best_e = th, e
    return best


def walk_forward(trades: list, start: int, end: int, train_days: int, test_days: int, use_filter: bool,
                 use_tm: bool = True, min_trades: int = 20, default_threshold: float = 0.55) -> dict:
    """Only the test periods are reported: they are the honest result."""
    wins = windows(start, end, train_days, test_days)
    rows, tested = [], []
    for a, b, c in wins:
        train = [t for t in trades if a <= t.trigger_time < b]
        test = [t for t in trades if b <= t.trigger_time < c]
        th = choose_threshold(train, use_tm, min_trades, default_threshold) if use_filter else None
        sel = [t for t in test if passes(t, th, use_tm=use_tm)]
        tested += sel
        m = metrics(sel, months=max((c - b) / (30.44 * 86400), 0.1))
        rows.append({"train_start": a, "test_start": b, "test_end": c, "threshold": th, **m})
    agg = metrics(tested, months=max(sum((r["test_end"] - r["test_start"]) for r in rows) / (30.44 * 86400), 0.1)
                  if rows else None)
    positive = [r for r in rows if r["trades"] and r["expectancy_r"] is not None and r["expectancy_r"] > 0]
    with_trades = [r for r in rows if r["trades"]]
    return {"windows": rows, "test": agg, "calibration": calibration(tested),
            "positive_windows": len(positive), "windows_with_trades": len(with_trades),
            "trades": [t.to_dict() for t in tested]}


def breakdown(trades: list) -> dict:
    out = {}
    keys = sorted({(t.pair, t.session) for t in trades})
    for p, s in keys:
        out["%s:%s" % (p, s)] = metrics([t for t in trades if t.pair == p and t.session == s])
    return out


def pass_criteria(wf: dict) -> dict:
    m = wf["test"]
    cal = [c for c in wf["calibration"] if c.get("n", 0) >= 10]
    cal_ok = all(abs(c["gap_points"]) <= 5 for c in cal) if cal else None
    most_positive = (wf["windows_with_trades"] > 0 and wf["positive_windows"] > wf["windows_with_trades"] / 2)
    return {"positive_expectancy": bool(m["expectancy_r"] is not None and m["expectancy_r"] > 0),
            "expectancy_above_0_1R": bool(m["expectancy_r"] is not None and m["expectancy_r"] >= 0.1),
            "profit_factor_above_1_3": bool(m["profit_factor"] is not None and m["profit_factor"] >= 1.3),
            "positive_in_most_windows": most_positive, "calibration_within_5_points": cal_ok,
            "enough_trades_per_month": m["trades_per_month"] >= 20}


__all__ = ["metrics", "calibration", "walk_forward", "breakdown", "pass_criteria", "windows", "Trade"]
