"""Yahoo prices, the feed's candle mode, the password login and the .env file."""
import asyncio
import json
import os
import time

import pytest
from conftest import flat_candles, ts
from fastapi.testclient import TestClient

from forexapp.api.server import create_app
from forexapp.bus import MemoryBus
from forexapp.config import Config, load_dotenv
from forexapp.data.candles import Candle, Tick
from forexapp.data.feed import FeedService
from forexapp.data.providers.yahoo import YahooProvider, parse_chart, yahoo_symbol
from forexapp.data.store import Store


def chart_json(start, n, price=1.1, live_extra=True):
    """A Yahoo v8 chart answer: n one-minute bars from ``start``, plus Yahoo's off-grid live point."""
    t = [start + 60 * i for i in range(n)]
    o = [price + 1e-4 * i for i in range(n)]
    c = [x + 5e-5 for x in o]
    h = [x + 1e-4 for x in o]
    lo = [x - 1e-4 for x in o]
    if live_extra:
        t.append(start + 60 * n + 17)
        for arr, v in ((o, c[-1]), (h, c[-1]), (lo, c[-1]), (c, c[-1] + 1e-5)):
            arr.append(v)
    o[2] = None                               # Yahoo sometimes leaves a field null
    return json.dumps({"chart": {"result": [{
        "meta": {"symbol": "EURUSD=X", "regularMarketPrice": c[-1], "regularMarketTime": t[-1]},
        "timestamp": t, "indicators": {"quote": [{"open": o, "high": h, "low": lo, "close": c, "volume": [0] * len(t)}]}}],
        "error": None}})


def test_symbols_and_parse():
    assert yahoo_symbol("EURUSD") == "EURUSD=X" and yahoo_symbol("USDJPY") == "JPY=X" and yahoo_symbol("XAUUSD") == "GC=F"
    d = parse_chart(chart_json(ts("2026-11-09 08:00"), 5), "EURUSD")
    assert len(d["bars"]) == 6 and d["price"] is not None and d["time"] == ts("2026-11-09 08:05") + 17
    t, o, h, l, c, v = d["bars"][2]
    assert o == pytest.approx(c) and h >= max(o, c) and l <= min(o, c)       # null open filled from the close
    with pytest.raises(Exception):
        parse_chart(json.dumps({"chart": {"result": None, "error": {"description": "No data found"}}}), "EURUSD")


class FakeYahoo(YahooProvider):
    """Answers like Yahoo would right now, without the network."""

    def __init__(self):
        super().__init__(poll_seconds=2)
        self.poll = 0.05
        self.calls = []

    async def fetch(self, pair, params):
        self.calls.append(params)
        now = int(time.time())
        if "period1" in params:
            a, b = int(params["period1"]), int(params["period2"])
            a -= a % 60
            return chart_json(a, max(0, (b - a) // 60), live_extra=False)
        start = now - now % 60 - 10 * 60
        return chart_json(start, 10)                   # 10 finished minutes + the live point


def test_stream_yields_finished_minutes_once_then_ticks():
    async def run():
        p = FakeYahoo()
        items = []
        async for x in p.stream(["EURUSD"]):
            items.append(x)
            if sum(isinstance(i, Tick) for i in items) == 2:
                break
        candles = [i for i in items if isinstance(i, Candle)]
        ticks = [i for i in items if isinstance(i, Tick)]
        assert len(candles) == 10                       # second poll: nothing new, no duplicates
        assert all(c.open_time % 60 == 0 for c in candles)
        assert ticks[0].ask > ticks[0].bid             # the pair's typical spread
        return p
    asyncio.run(run())


def test_history_clamps_to_30_days_in_chunks():
    async def run():
        p = FakeYahoo()
        now = int(time.time())
        rows = await p.history("EURUSD", now - 90 * 86400, now - now % 60)
        firsts = [c["period1"] for c in p.calls]
        assert min(firsts) >= now - 29 * 86400 - 5
        assert all(c["period2"] - c["period1"] <= 6 * 86400 for c in p.calls)
        assert rows and all(a.open_time < b.open_time for a, b in zip(rows, rows[1:]))
    asyncio.run(run())


def test_feed_stores_yahoo_candles_and_builds_5m():
    async def run():
        store = Store("sqlite://")
        bus = MemoryBus()
        sub = await bus.subscribe(["candles", "ticks"])
        feed = FeedService(FakeYahoo(), store, bus, ["EURUSD"], backfill_days=1)
        task = asyncio.create_task(feed.run())
        got_tick = False
        deadline = time.time() + 10
        while time.time() < deadline and not got_tick:
            try:
                ch, msg = await asyncio.wait_for(sub.get(), 1)
            except asyncio.TimeoutError:
                continue
            got_tick = ch == "ticks"
        feed.stop()
        task.cancel()
        assert got_tick
        assert store.candle_count("EURUSD", "1m") > 1000          # backfill: about one day of minutes
        assert store.candle_count("EURUSD", "5m") > 200
        last = store.last_candle_time("EURUSD", "1m")
        assert last >= int(time.time()) - 3 * 60
    asyncio.run(run())


# ---------------------------------------------------------------- privacy: password and .env
def test_password_turns_on_login_and_opens_to_the_network(monkeypatch, tmp_path):
    for k in ("FOREX_PASSWORD", "FOREX_HOST", "FOREX_AUTH_MODE", "FOREX_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("forexapp.config.APP_DIR", tmp_path)
    c = Config.from_env()
    assert c.auth_mode == "none" and c.host == "127.0.0.1"           # no password: this computer only
    monkeypatch.setenv("FOREX_PASSWORD", "correct-horse")
    c = Config.from_env()
    assert c.auth_mode == "token" and c.api_token == "correct-horse" and c.host == "0.0.0.0"
    assert c.public()["password"] is True                               # never echoed


def test_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nFX_TEST_A=1\nFX_TEST_B='two words'\nFX_TEST_C=set-elsewhere\n")
    monkeypatch.setenv("FX_TEST_C", "kept")
    load_dotenv(f)
    assert os.environ["FX_TEST_A"] == "1" and os.environ["FX_TEST_B"] == "two words" and os.environ["FX_TEST_C"] == "kept"
    for k in ("FX_TEST_A", "FX_TEST_B"):
        monkeypatch.delenv(k)


def test_login_check_and_lockout():
    cfg = Config()
    cfg.auth_mode, cfg.api_token = "token", "correct-horse"
    store = Store("sqlite://")
    store.upsert_candles(flat_candles("EURUSD", ts("2026-11-09 08:00"), 10))
    with TestClient(create_app(cfg, store, MemoryBus())) as c:
        assert c.get("/auth").json() == {"login_required": True, "mode": "token"}
        assert c.get("/auth/check").status_code == 401
        assert c.get("/auth/check", headers={"Authorization": "Bearer correct-horse"}).status_code == 200
        for _ in range(10):
            assert c.get("/auth/check", headers={"Authorization": "Bearer guess"}).status_code == 401
        # locked out now, even with the right password
        assert c.get("/auth/check", headers={"Authorization": "Bearer correct-horse"}).status_code == 429
        assert c.get("/").status_code == 200            # the page itself loads, and shows the login
