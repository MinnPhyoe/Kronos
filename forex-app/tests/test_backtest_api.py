import base64
import hashlib
import hmac
import json
import time

import pytest
from conftest import candle, flat_candles, make_forecast, ts
from fastapi.testclient import TestClient

from forexapp import instruments
from forexapp.api.auth import verify_hs256
from forexapp.api.server import create_app
from forexapp.backtest.engine import Trade, candidate_trades, simulate_outcome
from forexapp.backtest.metrics import metrics, walk_forward, windows
from forexapp.bus import MemoryBus
from forexapp.config import Config
from forexapp.data.store import Store


# ---------------------------------------------------------------- backtest
class SpyForecaster:
    name = "spy"

    def __init__(self):
        self.last_inputs = []

    def forecast(self, candles, pair, tf, horizon, n_paths):
        self.last_inputs.append(candles[-1].open_time)
        last = candles[-1]
        paths = [[(last.close, last.close + 0.0002 * (j + 1), last.close - 0.00005, last.close + 0.0002 * (j + 1))
                  for j in range(horizon)] for _ in range(n_paths)]
        return make_forecast(pair, last.open_time, last.close, paths)


def _day_with_breakout():
    t0 = ts("2026-11-09 08:00")
    hist = [c for c in flat_candles("EURUSD", t0 - 5 * 86400, 5 * 288, 1.1010, wiggle=0.0002) if c.open_time < t0]
    day = [candle("EURUSD", "5m", t0, 1.1010, 1.1015, 1.1000, 1.1012),
           candle("EURUSD", "5m", t0 + 300, 1.1012, 1.1015, 1.1004, 1.1010),
           candle("EURUSD", "5m", t0 + 600, 1.1010, 1.1013, 1.1003, 1.1011),
           candle("EURUSD", "5m", t0 + 900, 1.1011, 1.1019, 1.1010, 1.1018),     # trigger
           candle("EURUSD", "5m", t0 + 1200, 1.1020, 1.1024, 1.1017, 1.1022)]    # fill at its open
    p = 1.1022
    for i in range(30):
        day.append(candle("EURUSD", "5m", t0 + 1500 + 300 * i, p, p + 0.0003, p - 0.0001, p + 0.0002))
        p += 0.0002
    return hist + day, t0


def test_backtest_has_no_look_ahead_and_fills_next_open():
    cs, t0 = _day_with_breakout()
    cfg = Config()
    cfg.sessions = ["london"]
    cfg.tm_replays = 100
    spy = SpyForecaster()
    trades, _ = candidate_trades(cs, "EURUSD", cfg, ["london"], forecaster=spy, use_tm=False)
    t = [x for x in trades if x.day == "2026-11-09"][0]
    assert t.trigger_time == t0 + 1200 and t.fill_time == t0 + 1200
    # the forecast for this trade used candles up to the trigger candle only
    assert t0 + 900 in spy.last_inputs and t0 + 1200 not in spy.last_inputs
    ins = instruments.get("EURUSD")
    assert t.entry == pytest.approx(1.1020 + 0.00008 + cfg.slippage_pips * ins.pip)
    assert t.outcome == "tp1" and t.R == pytest.approx(1.0, abs=0.01)
    assert t.confidence == 1.0


def test_stop_first_when_both_touched_and_time_exit():
    ins = instruments.get("EURUSD")
    both = [candle("EURUSD", "5m", 0, 1.1, 1.103, 1.098, 1.1)]
    o, px, r = simulate_outcome(ins, "buy", 1.1, 1.099, 1.101, both, 0.001, 0.00002, 0.00008)
    assert o == "sl" and r < -1
    flat = [candle("EURUSD", "5m", 0, 1.1, 1.1003, 1.0997, 1.1002)]
    o, px, r = simulate_outcome(ins, "buy", 1.1, 1.099, 1.101, flat, 0.001, 0.0, 0.00008)
    assert o == "time" and r == pytest.approx(0.2)


def _trade(t, R, conf):
    return Trade("EURUSD", "london", "", "buy", t, t, 1, 0.9, 1.1, 1.2, 10, "tp1" if R > 0 else "sl", 0, R, conf,
                 0.6, 0.5, 0.1)


def test_walk_forward_picks_threshold_on_train_only():
    start = ts("2026-01-01 00:00")
    tr = []
    # training: only high-confidence trades win; test: the opposite (the threshold must not adapt to it)
    for i in range(40):
        tr.append(_trade(start + i * 3600, 1.0 if i % 2 == 0 else -1.0, 0.70 if i % 2 == 0 else 0.52))
    test_start = start + 10 * 86400
    for i in range(20):
        tr.append(_trade(test_start + i * 3600, -1.0, 0.70))
    wf = walk_forward(tr, start, test_start + 5 * 86400, 10, 5, use_filter=True, min_trades=5)
    assert wf["windows"][0]["threshold"] > 0.52          # excludes the losing 52% trades
    assert wf["test"]["trades"] == 20 and wf["test"]["expectancy_r"] == -1.0


def test_metrics_and_windows():
    m = metrics([_trade(i, r, None) for i, r in enumerate([1, -1, 1, 1, -1])], months=1)
    assert m["expectancy_r"] == 0.2 and m["profit_factor"] == 1.5 and m["max_drawdown_r"] == 1.0
    w = windows(0, 400 * 86400, 365, 91)
    assert len(w) == 1 and w[0][1] == 365 * 86400


# ---------------------------------------------------------------- API
@pytest.fixture
def app_env():
    cfg = Config()
    cfg.pairs = ["EURUSD"]
    store = Store("sqlite://")
    bus = MemoryBus()
    t0 = ts("2026-11-09 08:00")
    store.upsert_candles(flat_candles("EURUSD", t0, 50))
    return cfg, store, bus, t0


def test_rest_endpoints(app_env):
    cfg, store, bus, t0 = app_env
    with TestClient(create_app(cfg, store, bus)) as c:
        r = c.get("/candles", params={"pair": "EURUSD", "tf": "5m", "limit": 10})
        assert r.status_code == 200 and len(r.json()["candles"]) == 10
        assert c.get("/candles", params={"pair": "NOPE"}).status_code == 400
        assert c.get("/forecast", params={"pair": "EURUSD"}).json()["available"] is False
        f = make_forecast("EURUSD", t0 + 300 * 40, 1.1, [[(1.1, 1.1001, 1.0999, 1.1)] * 4] * 3)
        store.save_forecast(f.to_dict(include_paths=True))
        fr = c.get("/forecast", params={"pair": "EURUSD", "tf": "5m"}).json()
        assert fr["stale"] is True and "paths" not in fr and len(fr["band"]) == 4
        store.save_signal({"id": "EURUSD:london:2026-11-09", "pair": "EURUSD", "setup": "London ORB", "side": "buy",
                           "status": "triggered", "shown": True, "created_ts": t0})
        assert len(c.get("/signals", params={"status": "active"}).json()["signals"]) == 1
        r = c.post("/signals/EURUSD:london:2026-11-09/action", json={"action": "took"})
        assert r.json()["user_action"] == "took"
        assert c.post("/signals/EURUSD:london:2026-11-09/action", json={"action": "buy"}).status_code == 400
        assert c.put("/settings", json={"max_loss_per_trade": 80, "max_daily_loss": 200}).json()["max_loss_per_trade"] == 80
        assert c.put("/settings", json={"max_daily_loss": 10}).status_code == 400
        h = c.get("/health").json()
        assert h["model_up"] is False and h["signals_available"] is False
        assert c.get("/").status_code == 200


def test_token_auth_and_websocket(app_env):
    cfg, store, bus, _ = app_env
    cfg.auth_mode, cfg.api_token = "token", "s3cret"
    with TestClient(create_app(cfg, store, bus)) as c:
        assert c.get("/candles").status_code == 401
        assert c.get("/candles", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        with c.websocket_connect("/stream?token=s3cret") as ws:
            assert ws.receive_json()["type"] == "hello"


def _jwt(payload, secret):
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    h = enc(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = enc(json.dumps(payload).encode())
    s = enc(hmac.new(secret.encode(), (h + "." + p).encode(), hashlib.sha256).digest())
    return h + "." + p + "." + s


def test_supabase_jwt():
    tok = _jwt({"sub": "user-1", "aud": "authenticated", "exp": time.time() + 60}, "k")
    assert verify_hs256(tok, "k")["sub"] == "user-1"
    with pytest.raises(ValueError):
        verify_hs256(tok, "wrong")
    with pytest.raises(ValueError):
        verify_hs256(_jwt({"sub": "u", "exp": time.time() - 1}, "k"), "k")
