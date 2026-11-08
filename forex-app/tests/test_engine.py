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
        r = c.post("/orb", json={"pair": "EURUSD", "candles": mins, "sessions": ["london"]}).json()
        s = r["setups"][0]
        assert s["session"] == "london" and s["status"] == "triggered" and s["entry_state"]["side"] == "buy"
        plan = s["plans"][0]
        assert plan["stop"] < plan["entry"] < plan["tp1"]


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
