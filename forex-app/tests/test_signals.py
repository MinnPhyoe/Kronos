import asyncio

import numpy as np
import pandas as pd
import pytest
from conftest import candle, flat_candles, make_forecast, ts

from forexapp.bus import MemoryBus
from forexapp.config import Config, UserSettings
from forexapp.data.store import Store
from forexapp.models.kronos_service import KronosForecaster
from forexapp.signals.engine import SignalContext, build_signal, path_outcomes
from forexapp.strategy.alerts import Alerter
from forexapp.strategy.news import Calendar
from forexapp.strategy.scanner import OpeningRange
from forexapp.strategy.service import StrategyService, follow_outcome

SPEC_KEYS = {"pair", "side", "setup", "status", "entry", "sl", "tp1", "tp2", "kronos_confidence", "timemachine_odds",
             "expectancy_r", "risk_check", "lots", "forecast_band", "created_at"}


def _paths(n_tp, n_sl, n_none, entry=1.1020, step=0.0004, h=12):
    """Paths starting at 1.1016 (bid): up-paths hit TP, down-paths hit SL, flat ones neither."""
    out = []
    for kind, n in (("tp", n_tp), ("sl", n_sl), ("none", n_none)):
        for _ in range(n):
            p, path = 1.1016, []
            for j in range(h):
                d = step if kind == "tp" else -step if kind == "sl" else 0.0
                c = p + d
                path.append((p, max(p, c) + 0.00002, min(p, c) - 0.00002, c))
                p = c
            out.append(path)
    return out


def test_confidence_counts_tp_first_paths():
    f = make_forecast("EURUSD", ts("2026-11-09 08:25"), 1.1016, _paths(20, 8, 2))
    po = path_outcomes(f, "buy", 1.1020, 1.1005, 1.1035, 0.0001)
    assert (po["tp_first"], po["sl_first"], po["neither"]) == (20, 8, 2)
    assert po["confidence"] == pytest.approx(20 / 30, abs=1e-4)


def test_candle_touching_both_counts_as_stop():
    path = [[(1.1016, 1.1050, 1.1000, 1.1016)]]
    f = make_forecast("EURUSD", ts("2026-11-09 08:25"), 1.1016, path)
    po = path_outcomes(f, "buy", 1.1020, 1.1005, 1.1035, 0.0001)
    assert po["sl_first"] == 1 and po["tp_first"] == 0


def test_waiting_paths_must_break_out_first():
    flat = [[(1.1016, 1.1018, 1.1014, 1.1016)] * 5]
    f = make_forecast("EURUSD", ts("2026-11-09 08:25"), 1.1016, flat)
    po = path_outcomes(f, "buy", 1.1020, 1.1005, 1.1035, 0.0001, trigger_level=1.1019)
    assert po["entered"] == 0 and po["confidence"] is None


def _ctx(forecast, history_days=40, **kw):
    t0 = ts("2026-11-09 08:00")
    hist = flat_candles("EURUSD", t0 - history_days * 86400, history_days * 288, price=1.1010, wiggle=0.0003)
    hist = [c for c in hist if c.open_time < t0]
    rng_c = [candle("EURUSD", "5m", t0, 1.1010, 1.1015, 1.1000, 1.1012),
             candle("EURUSD", "5m", t0 + 300, 1.1012, 1.1015, 1.1004, 1.1010),
             candle("EURUSD", "5m", t0 + 600, 1.1010, 1.1013, 1.1003, 1.1011),
             candle("EURUSD", "5m", t0 + 900, 1.1011, 1.1018, 1.1009, 1.1016)]
    rng = OpeningRange("EURUSD", "london", "London ORB", "2026-11-09", t0, t0 + 900, t0 + 900 + 5400, 1.1015, 1.1000, 3)
    cfg = Config()
    cfg.tm_replays = 300
    cfg.horizon = 12
    args = dict(cfg=cfg, settings=UserSettings(), rng=rng, side="buy", status="triggered", level=1.1016,
                now=t0 + 1200, candles_5m=hist + rng_c, spread=0.0001, spread_normal=0.0001, forecast=forecast,
                trigger_time=t0 + 1200)
    args.update(kw)
    return SignalContext(**args)


def test_signal_json_has_the_spec_fields_and_passes():
    f = make_forecast("EURUSD", ts("2026-11-09 08:15"), 1.1016, _paths(26, 3, 1, step=0.0006))
    sig = build_signal(_ctx(f))
    assert SPEC_KEYS <= set(sig)
    assert sig["entry"] == pytest.approx(1.1017)                         # bid close + spread
    assert sig["sl"] < sig["entry"] < sig["tp1"] <= sig["tp2"]
    assert sig["kronos_confidence"] > 0.8 and sig["expectancy_r"] > 0
    assert sig["risk_check"] == "PASS" and sig["lots"] > 0
    names = {x["name"]: x["ok"] for x in sig["filters"]}
    assert names["forecast"] and names["confidence"] and names["risk"] and names["plan"]
    assert len(sig["forecast_band"]) == 12 and {"time", "p10", "p50", "p90"} <= set(sig["forecast_band"][0])


def test_model_down_marks_signal_unavailable():
    sig = build_signal(_ctx(None, forecast_error="model timeout"))
    assert sig["status"] == "filtered" and not sig["shown"]
    assert sig["model"]["available"] is False
    assert any("model unavailable" in r for r in sig["reasons"])


def test_low_confidence_is_filtered():
    f = make_forecast("EURUSD", ts("2026-11-09 08:15"), 1.1016, _paths(14, 14, 2, step=0.0006))
    sig = build_signal(_ctx(f))
    assert not sig["passed"] and sig["status"] == "filtered" and not sig["shown"]
    conf = [x for x in sig["filters"] if x["name"] in ("confidence", "plan")]
    assert conf and not all(x["ok"] for x in conf)


def test_follow_outcome():
    sig = {"side": "buy", "entry": 1.1017, "sl": 1.1004, "tp1": 1.1030, "trigger": {"time": "2026-11-09T08:20:00Z"},
           "kronos": {"cost_r": 0.0}}
    t = ts("2026-11-09 08:21")
    assert follow_outcome(sig, {"open_time": t, "high": 1.1020, "low": 1.1010, "close": 1.1015, "spread": 1e-4}, 24) is None
    o = follow_outcome(sig, {"open_time": t, "high": 1.1031, "low": 1.1010, "close": 1.1015, "spread": 1e-4}, 24)
    assert o == ("tp1", 1.1030, 1.0)
    o = follow_outcome(sig, {"open_time": t, "high": 1.1031, "low": 1.1003, "close": 1.1015, "spread": 1e-4}, 24)
    assert o[0] == "sl" and o[2] == -1.0


# ---------------------------------------------------------------- Kronos wrapper (fake predictor)
class FakePredictor:
    def __init__(self):
        self.calls = []

    def predict_batch(self, dfs, xts, yts, pred_len, **kw):
        self.calls.append((len(dfs), dfs[0].copy(), list(yts[0]), kw))
        out = []
        for i, (df, y) in enumerate(zip(dfs, yts)):
            last = df["close"].iloc[-1]
            vals = np.array([[last, last + 0.001 * (i + 1), last - 0.002, last + 0.0001 * i, 0, 0]] * pred_len)
            out.append(pd.DataFrame(vals, columns=["open", "high", "low", "close", "volume", "amount"], index=y))
        return out


def test_kronos_wrapper_keeps_every_path_and_skips_weekend():
    cfg = Config()
    k = KronosForecaster(cfg)
    k.predictor = FakePredictor()
    start = ts("2026-11-13 21:55") - 499 * 300            # ends Friday 16:55 New York
    cs = flat_candles("EURUSD", start, 500)
    f = k.forecast(cs, "EURUSD", "5m", 6, 30)
    n, df, ys, kw = k.predictor.calls[0]
    assert n == 30 and kw["sample_count"] == 1 and kw["T"] == 1.0 and kw["top_p"] == 0.9
    assert len(df) == cfg.lookback and (df["amount"] == 0).all() and (df["volume"] == 10).all()
    assert f.paths.shape == (30, 6, 4)
    assert f.times[0] == ts("2026-11-15 22:00")            # the Sunday open, no weekend candles
    assert len({round(p[-1][3], 6) for p in f.paths}) == 30    # paths are kept, not averaged
    assert (f.paths[:, :, 1] >= f.paths[:, :, 3]).all()


# ---------------------------------------------------------------- the strategy service, end to end
class FakeModel:
    """Answers forecast requests with paths that rise from the requested candle."""

    def __init__(self, store):
        self.store = store

    def reply(self, req):
        cs = self.store.get_candles(req["pair"], "5m", 1, int(req["base_time"]) + 1)
        last = cs[-1]
        paths = []
        for i in range(30):
            p, path = last.close, []
            for j in range(24):
                d = 0.0004 if i < 28 else -0.0004      # 2 of 30 fall: under the 10th percentile
                c = p + d
                path.append((p, max(p, c) + 1e-5, min(p, c) - 1e-5, c))
                p = c
            paths.append(path)
        f = make_forecast(req["pair"], last.open_time, last.close, paths)
        return {"type": "forecast", "request_id": req["id"], "ok": True, "forecast": f.to_dict(include_paths=True)}


def test_strategy_service_from_range_to_triggered_signal():
    async def run():
        store = Store("sqlite://")
        bus = MemoryBus()
        cfg = Config()
        cfg.tm_replays = 200
        cfg.pairs = ["EURUSD"]
        cfg.sessions = ["london"]
        store.put_settings(UserSettings(pairs=["EURUSD"], sessions=["london"]).to_dict())
        t0 = ts("2026-11-09 08:00")
        # a steadily rising pool, so the Time Machine agrees with the forecast
        hist = [c for c in flat_candles("EURUSD", t0 - 30 * 86400, 30 * 288, 1.1010, wiggle=0.0003, drift=0.0001)
                if c.open_time < t0]
        hist = [c.__class__(c.pair, c.tf, c.open_time, c.open - hist[-1].close + 1.1010, c.high - hist[-1].close + 1.1010,
                            c.low - hist[-1].close + 1.1010, c.close - hist[-1].close + 1.1010, 10, 0.00008) for c in hist]
        rng_c = [candle("EURUSD", "5m", t0, 1.1010, 1.1015, 1.1000, 1.1012),
                 candle("EURUSD", "5m", t0 + 300, 1.1012, 1.1015, 1.1004, 1.1010),
                 candle("EURUSD", "5m", t0 + 600, 1.1010, 1.1013, 1.1003, 1.1011)]
        store.upsert_candles(hist + rng_c)
        sub = await bus.subscribe(["forecast_requests", "signals", "alerts"])
        svc = StrategyService(cfg, store, bus, Alerter(bus), Calendar(None, None))
        model = FakeModel(store)
        await svc.on_5m_close("EURUSD", rng_c[-1].to_dict())          # the range is complete
        ch, req = await sub.get()
        assert ch == "forecast_requests" and req["base_time"] == rng_c[-1].open_time
        await svc.on_forecast(model.reply(req))
        msgs = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        waiting = [m["signal"] for c, m in msgs if c == "signals"][-1]
        assert waiting["status"] == "waiting" and waiting["side"] == "buy" and waiting["shown"]
        assert waiting["entry"] == pytest.approx(1.1015 + 0.00008)
        # a 5m candle closes above the range high: triggered
        trig = candle("EURUSD", "5m", t0 + 900, 1.1011, 1.1019, 1.1010, 1.1018)
        store.upsert_candles([trig])
        await svc.on_5m_close("EURUSD", trig.to_dict())
        ch, req = await sub.get()
        assert ch == "forecast_requests" and req["base_time"] == trig.open_time   # input ends at the trigger candle
        await svc.on_forecast(model.reply(req))
        msgs = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        sig = [m["signal"] for c, m in msgs if c == "signals"][-1]
        assert sig["status"] == "triggered" and sig["passed"], sig["reasons"]
        assert sig["trigger"]["price"] == 1.1018 and sig["entry"] == pytest.approx(1.10188)
        assert any(c == "alerts" for c, _ in msgs)
        assert store.get_signal(sig["id"])["status"] == "triggered"
        # the next 1m candle reaches TP1: the results log records the outcome
        await svc.on_1m_close("EURUSD", {"open_time": t0 + 1200, "open": 1.1018, "high": sig["tp1"] + 1e-5,
                                         "low": 1.1017, "close": sig["tp1"], "spread": 0.00008})
        done = store.get_signal(sig["id"])
        assert done["status"] == "closed" and done["outcome"] == "tp1" and done["outcome_r"] > 0.9
    asyncio.run(run())
