"""The Kronos + Day Trader engine that OBSIDIAN calls, and its client."""
import socket
import threading
import time

import numpy as np
import pytest
from conftest import ts
from fastapi.testclient import TestClient

from forexapp.config import TIMEFRAMES, Config
from forexapp.engine_client import EngineClient, EngineError, from_oanda
from forexapp.engine_server import create_engine_app
from forexapp.models.forecast import Forecast
from forexapp.timeutil import iso, next_candle_times


class Steered:
    """A stand-in model whose paths rise (or fall) from the last candle, so outcomes are known."""
    name = "steered test model"

    def __init__(self, up=27, n_down=3, step=0.0004):
        self.up, self.down, self.step = up, n_down, step

    def ready(self):
        return True

    def forecast(self, candles, pair, tf, horizon, n_paths):
        last = candles[-1]
        paths = []
        for i in range(n_paths):
            d = self.step if i < self.up * n_paths // (self.up + self.down) else -self.step
            p, path = last.close, []
            for _ in range(horizon):
                c = p + d
                path.append((p, max(p, c) + 1e-5, min(p, c) - 1e-5, c))
                p = c
            paths.append(path)
        times = next_candle_times(last.open_time, TIMEFRAMES[tf], horizon)
        return Forecast(pair, tf, last.open_time, last.close, times, np.asarray(paths), self.name)


def oanda_candles(start, n, step=900, price=1.1, drift=0.00005):
    """Candles the way OANDA's v20 /candles answers (nanosecond RFC3339 times, bid and ask)."""
    out, p = [], price
    rng = np.random.default_rng(3)
    for i in range(n):
        c = p + drift + rng.normal(0, 0.0003)
        h, l = max(p, c) + 0.0002, min(p, c) - 0.0002
        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start + i * step)) + ".000000000Z"
        bid = {"o": "%.5f" % p, "h": "%.5f" % h, "l": "%.5f" % l, "c": "%.5f" % c}
        ask = {k: "%.5f" % (float(v) + 0.00008) for k, v in bid.items()}
        out.append({"complete": True, "volume": 120, "time": t, "bid": bid, "ask": ask})
        p = c
    out.append({"complete": False, "volume": 3, "time": "2030-01-01T00:00:00.000000000Z", "bid": bid, "ask": ask})
    return out


START = ts("2026-11-02 00:00")


def test_from_oanda_drops_the_forming_candle_and_reads_spread():
    rows = from_oanda(oanda_candles(START, 5), price="bid")
    assert len(rows) == 5 and rows[0]["spread"] == pytest.approx(0.00008) and rows[0]["time"].endswith("Z")


def test_health_forecast_and_cache():
    app = create_engine_app(Config(), forecaster=Steered(), token="")
    with TestClient(app) as c:
        h = c.get("/health").json()
        assert h["ok"] and h["model"] == "steered test model" and "4h" in h["timeframes"]
        body = {"pair": "EUR_USD", "timeframe": "M15", "candles": from_oanda(oanda_candles(START, 300), "bid"), "horizon": 8}
        f = c.post("/forecast", json=body).json()
        assert f["pair"] == "EURUSD" and f["tf"] == "15m" and len(f["band"]) == 8 and len(f["median_path"]) == 8
        assert f["direction_prob"] == pytest.approx(0.9) and f["cached"] is False
        assert c.post("/forecast", json=body).json()["cached"] is True
        assert c.post("/forecast", json={**body, "timeframe": "W1"}).status_code == 400
        assert c.post("/forecast", json={**body, "candles": body["candles"][:20]}).status_code == 400


def test_check_passes_a_plan_the_paths_support_and_blocks_one_they_dont():
    rows = from_oanda(oanda_candles(START, 400, drift=0.00006), "bid")
    last = rows[-1]["close"]
    body = {"pair": "EURUSD", "timeframe": "M15", "candles": rows, "side": "buy", "entry": round(last + 0.0001, 5),
            "stop": round(last - 0.0015, 5), "target": round(last + 0.0017, 5), "horizon": 24, "replays": 500}
    with TestClient(create_engine_app(Config(), forecaster=Steered(up=27, n_down=3), token="")) as c:
        r = c.post("/check", json=body).json()
        assert r["kronos"]["confidence"] == pytest.approx(0.9) and r["kronos"]["avg_R"] > 0
        assert r["timemachine"]["replays"] == 500 and "verdict" in r["timemachine"]
        assert r["spread_pips"] == pytest.approx(0.8)                       # from the OANDA bid/ask
        assert {f["name"] for f in r["filters"]} == {"kronos_confidence", "kronos_expectancy", "timemachine"}
        assert r["blocks"] == (not all(f["ok"] for f in r["filters"]))
        bad = c.post("/check", json={**body, "stop": body["entry"] + 0.001}).status_code
        assert bad == 400
    with TestClient(create_engine_app(Config(), forecaster=Steered(up=3, n_down=27), token="")) as c:
        r = c.post("/check", json=body).json()
        assert r["blocks"] and r["kronos"]["confidence"] < 0.2
        assert any("forecast paths reach the target" in x for x in r["reasons"])


def test_daily_candles_skip_the_weekend_and_time_machine_runs():
    rows = from_oanda(oanda_candles(ts("2026-01-04 22:00"), 300, step=86400, drift=0.0004), "mid")
    rows = [r for r in rows if time.gmtime(int(time.mktime(time.strptime(r["time"][:19], "%Y-%m-%dT%H:%M:%S")))).tm_wday != 5]
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        last = rows[-1]["close"]
        r = c.post("/check", json={"pair": "EURUSD", "timeframe": "D1", "candles": rows, "side": "buy", "entry": last,
                                   "stop": last - 0.01, "target": last + 0.01, "horizon": 5, "replays": 300}).json()
        assert "error" not in r["timemachine"], r["timemachine"]


def test_orb_from_one_minute_candles():
    t0 = ts("2026-11-09 07:30")
    mins = []
    p = 1.1000
    for i in range(120):                                          # 07:30 to 09:30 UTC
        t = t0 + 60 * i
        d = 0.00012 if t >= ts("2026-11-09 08:15") else (0.00003 if i % 2 else -0.00003)
        c = p + d
        mins.append({"time": t, "open": p, "high": max(p, c) + 0.00002, "low": min(p, c) - 0.00002, "close": c})
        p = c
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        at_trigger = [m for m in mins if m["time"] < ts("2026-11-09 08:20")]      # up to the breakout candle's close
        r = c.post("/orb", json={"pair": "EURUSD", "candles": at_trigger, "sessions": ["london"]}).json()
        s = r["setups"][0]
        assert s["session"] == "london" and s["status"] == "triggered" and s["entry_state"]["side"] == "buy"
        plan = s["plans"][0]
        assert plan["stop"] < plan["entry"] < plan["tp1"]
        assert "outcome" not in s
        # an hour later price has climbed through the target: the setup is finished, not a trade any more
        s = c.post("/orb", json={"pair": "EURUSD", "candles": mins, "sessions": ["london"]}).json()["setups"][0]
        assert s["status"] == "closed" and s["outcome"]["result"] == "target"
        assert ts("2026-11-09 08:20") < s["outcome"]["time"] <= ts("2026-11-09 09:30")


def test_first_touch_counts_the_stop_when_one_candle_touches_both():
    from forexapp.data.candles import Candle
    from forexapp.engine_server import first_touch
    plan = {"side": "sell", "stop": 1.1030, "tp1": 1.0990}
    calm = Candle("EURUSD", "5m", 600, 1.1010, 1.1020, 1.1000, 1.1010)
    wild = Candle("EURUSD", "5m", 900, 1.1010, 1.1035, 1.0985, 1.1000)
    assert first_touch([calm], 600, 900, plan) == (None, None)
    assert first_touch([calm, wild], 600, 1200, plan) == ("stop", 1200)
    assert first_touch([Candle("EURUSD", "5m", 600, 1.1, 1.1005, 1.0989, 1.0995)], 600, 900, plan) == ("target", 900)


def test_token_and_client_round_trip():
    app = create_engine_app(Config(), forecaster=Steered(), token="s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 401
        assert c.get("/health", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        eng = EngineClient("http://127.0.0.1:%d" % port, token="s3cret")
        assert eng.health()["ok"]
        rows = from_oanda(oanda_candles(START, 200), "bid")
        assert len(eng.forecast("EUR_USD", "M15", rows, horizon=6)["band"]) == 6
        with pytest.raises(EngineError) as e:
            EngineClient("http://127.0.0.1:%d" % port, token="wrong").health()
        assert e.value.status == 401
    finally:
        server.should_exit = True
        th.join(5)
    assert iso(START)


# ---------------------------------------------------------------- the best result: two risk engines + Kronos + Time Machine
def _orb_minutes(start, end, breakout_at, price=1.1000):
    """1-minute candles that wiggle, then climb after ``breakout_at``."""
    rows, p, t, i = [], price, start, 0
    while t < end:
        d = 0.00012 if t >= breakout_at else (0.00003 if i % 2 else -0.00003)
        c = p + d
        rows.append({"time": t, "open": p, "high": max(p, c) + 0.00002, "low": min(p, c) - 0.00002, "close": c,
                     "spread": 0.00008})
        p, t, i = c, t + 60, i + 1
    return rows


def _best_body(**kw):
    m15 = from_oanda(oanda_candles(START, 400, drift=0.00006), "bid")
    last = m15[-1]["close"]
    body = {
        "markets": [{"pair": "EUR_USD", "timeframe": "M15", "candles": m15},
                    {"pair": "GBP_USD", "timeframe": "M15", "candles": from_oanda(oanda_candles(START, 400, price=1.27), "bid")}],
        "candidates": [
            {"name": "London order block", "pair": "EUR_USD", "timeframe": "M15", "side": "buy",
             "entry": round(last + 0.0001, 5), "stop": round(last - 0.0015, 5), "target": round(last + 0.0017, 5),
             "obsidian_risk": "PASS"},
            {"name": "Bearish breaker", "pair": "EUR_USD", "timeframe": "M15", "side": "sell",
             "entry": round(last, 5), "stop": round(last + 0.0016, 5), "target": round(last - 0.0016, 5),
             "obsidian_risk": "PASS"}],
        "include_orb": False,
        "account": {"balance": 10000, "currency": "GBP", "risk_per_trade_pct": 0.5, "daily_loss_pct": 2.0},
        "replays": 400}
    body.update(kw)
    return body


def test_best_is_the_setup_that_passes_both_risk_engines_and_the_forecast():
    with TestClient(create_engine_app(Config(), forecaster=Steered(up=27, n_down=3), token="")) as c:
        r = c.post("/best", json=_best_body()).json()
        assert r["counts"] == {"candidates": 2, "eligible": 1}
        sell = next(x for x in r["ranked"] if x["side"] == "sell")
        assert not sell["eligible"] and any("forecast paths reach the target" in w for w in sell["why_not"])
        buy = next(x for x in r["ranked"] if x["side"] == "buy")
        dt = buy["checks"]["daytrader_risk"]
        assert dt["currency"] == "GBP" and dt["max_loss"] <= 50.0 and dt["lots"] > 0        # 0.5% of 10,000 GBP
        assert buy["checks"]["obsidian_risk"]["verdict"] == "PASS"
        assert buy["eligible"] and r["best"]["name"] == "London order block"      # the history trends up, like the paths
        assert r["message"].startswith("Best: EURUSD buy")
        assert buy["score_R"] == min(buy["checks"]["kronos"]["avg_R"], buy["checks"]["timemachine"]["avg_R"])
        assert r["ranked"][0] is not None and r["ranked"][0]["side"] == "buy"


def test_obsidian_block_is_never_overridden():
    body = _best_body()
    for cand in body["candidates"]:
        cand["obsidian_risk"], cand["obsidian_reasons"] = "BLOCK", ["daily loss limit used"]
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        r = c.post("/best", json=body).json()
        assert r["best"] is None and all(not x["eligible"] for x in r["ranked"])
        assert any("OBSIDIAN risk engine: daily loss limit used" in w for w in r["ranked"][0]["why_not"])


def test_unverifiable_currency_blocks_in_the_day_trader_check():
    body = _best_body()
    body["markets"] = body["markets"][:1]                     # no GBPUSD price: GBP sizing cannot be verified
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        r = c.post("/best", json=body).json()
        buy = next(x for x in r["ranked"] if x["side"] == "buy")
        assert buy["checks"]["daytrader_risk"]["verdict"] == "BLOCK" and not buy["eligible"]
        assert "cannot verify" in buy["checks"]["daytrader_risk"]["reasons"][0]
        body["rates"] = {"GBP_USD": 1.27}                          # given as a rate instead: verifiable again
        buy = next(x for x in c.post("/best", json=body).json()["ranked"] if x["side"] == "buy")
        assert buy["checks"]["daytrader_risk"]["verdict"] == "PASS"


def test_day_trader_setups_join_and_agreement_is_shown():
    orb = _orb_minutes(ts("2026-11-05 20:00"), ts("2026-11-06 08:20"), ts("2026-11-06 08:15"))   # up to the breakout
    body = _best_body(include_orb=True, sessions=["london"])
    body["markets"][0]["orb_candles"] = orb
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        r = c.post("/best", json=body).json()
        dtr = [x for x in r["ranked"] if x["source"] == "Day Trader"]
        assert dtr and dtr[0]["status"] == "triggered" and dtr[0]["side"] == "buy"
        assert dtr[0]["checks"]["obsidian_risk"]["verdict"] == "NOT CHECKED" and not dtr[0]["eligible"]
        assert "OBSIDIAN: London order block" in dtr[0]["agrees_with"]
        assert r["counts"]["candidates"] == 3


def test_a_breakout_that_already_hit_its_target_is_not_offered_again():
    orb = _orb_minutes(ts("2026-11-05 20:00"), ts("2026-11-06 09:30"), ts("2026-11-06 08:15"))  # climbs on and on
    body = _best_body(include_orb=True, sessions=["london"])
    body["markets"][0]["orb_candles"] = orb
    with TestClient(create_engine_app(Config(), forecaster=Steered(), token="")) as c:
        r = c.post("/best", json=body).json()
        assert not [x for x in r["ranked"] if x["source"] == "Day Trader"]
