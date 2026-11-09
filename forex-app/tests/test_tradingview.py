"""The TradingView script for a signal, its API route, and on-demand analysis under /engine."""
import re

import pytest
from conftest import flat_candles, ts
from fastapi.testclient import TestClient

from forexapp.api.server import create_app
from forexapp.bus import MemoryBus
from forexapp.config import Config
from forexapp.data.store import Store
from forexapp.models.bootstrap import BootstrapForecaster
from forexapp.tradingview import pine_for_signal, tradingview_url

SIG = {"id": "EURUSD:london:2026-11-09", "pair": "EURUSD", "side": "buy", "setup": "London ORB", "status": "triggered",
       "entry": 1.0852, "sl": 1.0838, "tp1": 1.0866, "tp2": 1.0881, "trigger": {"time": "2026-11-09T08:20:00Z"},
       "created_at": "2026-11-09T08:15:00Z", "shown": True, "created_ts": ts("2026-11-09 08:15")}


def test_pine_script_draws_the_levels():
    s = pine_for_signal(SIG, hold_minutes=120)
    assert s.startswith("//@version=6\n")
    assert 't0 = timestamp("UTC", 2026, 11, 9, 8, 20)' in s
    for v in ("entry = 1.08520", "stop = 1.08380", "tp1 = 1.08660", "tp2 = 1.08810"):
        assert v in s
    assert '"SL 1.08380 (14.0 pips)"' in s and '"TP1 1.08660 (14.0 pips)"' in s and '"TP2 1.08810 (29.0 pips)"' in s
    assert s.count("box.new(") == 2 and s.count("line.new(") == 4 and 'str.contains(syminfo.ticker, "EURUSD")' in s
    for line in s.splitlines():                      # Pine blocks are 4-space indented; brackets balance per line
        assert (len(line) - len(line.lstrip(" "))) % 4 == 0
        assert line.count("(") == line.count(")"), line
        assert line.count('"') % 2 == 0, line


def test_sell_and_jpy_digits_and_quotes():
    s = pine_for_signal({**SIG, "pair": "USDJPY", "side": "sell", "entry": 151.2, "sl": 151.5, "tp1": 150.9, "tp2": 150.6,
                         "setup": 'New "York" ORB'})
    assert "entry = 151.200" in s and '"Sell 151.200"' in s and "New 'York' ORB" in s
    assert "zoneTop = 150.600" in s                     # the profit zone reaches the farther target
    with pytest.raises(ValueError):
        pine_for_signal({**SIG, "sl": None})
    assert tradingview_url("XAUUSD").endswith("OANDA:XAUUSD")


def test_tradingview_route_and_engine_mount():
    store = Store("sqlite://")
    store.save_signal(SIG)
    store.upsert_candles(flat_candles("EURUSD", ts("2026-11-09 00:00"), 120))
    app = create_app(Config(), store, MemoryBus(), forecaster=BootstrapForecaster())
    with TestClient(app) as c:
        r = c.get("/signals/EURUSD:london:2026-11-09/tradingview").json()
        assert r["script"].startswith("//@version=6") and "Pine Editor" in r["how"]
        assert c.get("/signals/nope/tradingview").status_code == 404
        h = c.get("/engine/health").json()
        assert h["model"] == "bootstrap (not Kronos)"
        rows = c.get("/candles", params={"pair": "EURUSD", "tf": "5m", "limit": 120}).json()["candles"]
        body = {"pair": "EURUSD", "timeframe": "M5", "horizon": 6, "paths": 8,
                "candles": [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]}
        f = c.post("/engine/forecast", json=body).json()
        assert len(f["band"]) == 6 and f["model"] == "bootstrap (not Kronos)"
    assert re.search(r"London ORB", SIG["setup"])
