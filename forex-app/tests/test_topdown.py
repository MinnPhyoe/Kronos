"""Top-down analysis: higher timeframes give the direction, lower ones the entry."""
import math
import time

from conftest import ts
from fastapi.testclient import TestClient

from forexapp import instruments
from forexapp.api.server import create_app
from forexapp.bus import MemoryBus
from forexapp.config import Config
from forexapp.data.candles import Candle, resample
from forexapp.data.store import Store
from forexapp.strategy import topdown


def trending_minutes(pair, start, days, drift_per_min, price=1.1000):
    """Weekday 1-minute candles: a steady drift with a slow wave on top (pullbacks)."""
    out, p, t = [], price, start
    end = start + days * 86400
    i = 0
    while t < end:
        wd = time.gmtime(t).tm_wday
        if wd < 5:
            wave = 0.0004 * math.sin(i / 180.0)
            nxt = price + drift_per_min * i + wave
            out.append(Candle(pair, "1m", t, p, max(p, nxt) + 0.00004, min(p, nxt) - 0.00004, nxt, 5, 0.00008))
            p = nxt
            i += 1
        t += 60
    return out


def test_uptrend_reads_up_and_plans_a_buy():
    now = ts("2026-11-25 12:00")
    m1 = trending_minutes("EURUSD", now - 30 * 86400, 30, 0.000003)
    td = topdown.analyse("EURUSD", m1, instruments.get("EURUSD"), now)
    assert td["timeframes"]["4h"]["bias"] == "up" and td["timeframes"]["1h"]["bias"] == "up"
    assert td["side"] == "buy"
    p = td["plan"]
    assert p["stop"] < p["entry"] < p["target"]
    assert p["reward_risk"] >= 1.5
    assert td["status"] in ("enter now", "almost", "wait for the pullback", "wait")


def test_mixed_trends_give_no_trade():
    now = ts("2026-11-25 12:00")
    up = trending_minutes("EURUSD", now - 30 * 86400, 29, 0.000004)
    last = up[-1].close
    down = trending_minutes("EURUSD", up[-1].open_time + 60, 1, -0.00002, price=last)   # a sharp last day down
    td = topdown.analyse("EURUSD", up + down, instruments.get("EURUSD"), now)
    if td["timeframes"]["4h"]["bias"] != td["timeframes"]["1h"]["bias"]:
        assert td["side"] is None and td["status"] == "no trade"


def test_topdown_endpoint_ranks_and_grades():
    now = int(time.time()) // 60 * 60
    store = Store("sqlite://")
    m1 = trending_minutes("EURUSD", now - 32 * 86400, 32, 0.000003)
    store.upsert_candles(m1)
    for tf in ("5m", "15m", "1h"):
        store.upsert_candles(resample(m1, tf))
    cfg = Config()
    cfg.pairs = ["EURUSD"]
    cfg.tm_replays = 300
    with TestClient(create_app(cfg, store, MemoryBus(), engine=False)) as c:
        r = c.get("/topdown").json()
    assert r["results"] and r["results"][0]["pair"] == "EURUSD"
    res = r["results"][0]
    assert {"timeframes", "side", "status", "summary"} <= set(res)
    if res.get("plan"):
        assert "timemachine" in res and "risk_check" in res
        assert res["passed"] == (res["grade"] is not None)
