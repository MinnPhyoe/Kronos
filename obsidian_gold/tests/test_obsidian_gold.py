"""Offline tests for OBSIDIAN Gold.  python3 -m unittest discover -s obsidian_gold/tests -v"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import feeds  # noqa: E402
import server  # noqa: E402

NOW = 1793000000  # a Wednesday, 2026-10-26 07:33 UTC


def ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def flat(t0, n, price=2400.0, step=900):
    return [{"t": t0 + i * step, "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 1} for i in range(n)]


class Indicators(unittest.TestCase):
    def test_ema_seeds_with_sma_then_smooths(self):
        e = engine.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 3.0)
        self.assertAlmostEqual(e[4], 4.0)

    def test_rsi_extremes(self):
        self.assertEqual(engine.rsi(list(range(30)), 14)[-1], 100.0)
        self.assertLess(engine.rsi(list(range(30, 0, -1)), 14)[-1], 1)

    def test_atr_of_constant_range(self):
        cs = flat(0, 30)
        self.assertAlmostEqual(engine.atr(cs, 14)[-1], 2.0)

    def test_resample_groups_by_bucket(self):
        cs = [{"t": 0, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1},
              {"t": 900, "o": 1.5, "h": 3, "l": 1, "c": 2, "v": 2},
              {"t": 3600, "o": 2, "h": 2.5, "l": 1.8, "c": 2.2, "v": 3}]
        h = engine.resample(cs, 3600)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0], {"t": 0, "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 3})


class Sessions(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(engine.session_of(ts("2026-10-26 08:00")), "London")
        self.assertEqual(engine.session_of(ts("2026-10-26 13:00")), "New York")
        self.assertEqual(engine.session_of(ts("2026-10-26 03:00")), "Asia")
        self.assertIsNone(engine.session_of(ts("2026-10-26 20:00")))

    def test_next_session_skips_weekend(self):
        c = engine.next_session(ts("2026-10-24 12:00"))  # Saturday
        self.assertEqual(c["next"], "London")
        self.assertEqual(c["next_t"], ts("2026-10-26 07:00"))


class Signals(unittest.TestCase):
    def setUp(self):
        self.t0 = ts("2026-10-26 07:00")

    def _ind(self, cs):
        return engine.indicators(cs)

    def test_breakout_buy_with_stop_and_targets(self):
        cs = flat(self.t0 - 40 * 900, 40)
        cs.append({"t": self.t0, "o": 2400, "h": 2406, "l": 2399.5, "c": 2405.5, "v": 1})
        s = engine.detect(cs, len(cs) - 1, self._ind(cs), "bullish")
        self.assertEqual((s["side"], s["kind"], s["session"]), ("buy", "Breakout", "London"))
        self.assertEqual(s["entry"], 2405.5)
        self.assertLess(s["stop"], 2399.5)
        self.assertAlmostEqual(s["tp1"] - s["entry"], s["entry"] - s["stop"], places=1)
        self.assertAlmostEqual(s["tp2"] - s["entry"], 2 * (s["entry"] - s["stop"]), places=1)

    def test_no_trade_against_bias_or_outside_windows(self):
        cs = flat(self.t0 - 40 * 900, 40)
        cs.append({"t": self.t0, "o": 2400, "h": 2406, "l": 2399.5, "c": 2405.5, "v": 1})
        self.assertIsNone(engine.detect(cs, len(cs) - 1, self._ind(cs), "bearish"))
        self.assertIsNone(engine.detect(cs, len(cs) - 1, self._ind(cs), "neutral"))
        late = ts("2026-10-26 18:00")
        cs2 = flat(late - 40 * 900, 40)
        cs2.append({"t": late, "o": 2400, "h": 2406, "l": 2399.5, "c": 2405.5, "v": 1})
        self.assertIsNone(engine.detect(cs2, len(cs2) - 1, self._ind(cs2), "bullish"))

    def test_walk_stop_first_inside_one_candle(self):
        sig = {"side": "buy", "entry": 100, "stop": 98, "tp1": 102, "tp2": 104, "risk": 2}
        cs = [{"t": 0, "o": 100, "h": 105, "l": 97, "c": 100}]
        self.assertEqual(engine.walk(cs, 0, sig)["result"], "stop")

    def test_walk_tp1_then_breakeven_then_tp2(self):
        sig = {"side": "sell", "entry": 100, "stop": 102, "tp1": 98, "tp2": 96, "risk": 2}
        cs = [{"t": 0, "o": 100, "h": 100.5, "l": 97.5, "c": 98},
              {"t": 900, "o": 98, "h": 98.5, "l": 95.5, "c": 96}]
        o = engine.walk(cs, 0, sig)
        self.assertEqual((o["result"], o["r"], o["tp1"]), ("tp2", 2.0, True))

    def test_walk_open_and_timeout(self):
        sig = {"side": "buy", "entry": 100, "stop": 98, "tp1": 102, "tp2": 104, "risk": 2}
        cs = flat(0, 5, price=100.5)
        self.assertEqual(engine.walk(cs, 0, sig)["result"], "open")
        cs = flat(0, 40, price=101)
        o = engine.walk(cs, 0, sig)
        self.assertEqual(o["result"], "timeout")
        self.assertAlmostEqual(o["r"], 0.5)


class WholeAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = feeds.demo(now=NOW)
        cls.a = engine.analyse(cls.cs, balance=5000, risk_pct=1)

    def test_shape(self):
        a = self.a
        self.assertTrue(a["ok"])
        for k in ("bias", "checks", "history", "stats", "levels", "projection", "clock"):
            self.assertIn(k, a)
        self.assertGreater(a["stats"]["all"]["trades"], 10)
        cone = a["projection"]["cone"]
        self.assertEqual(len(cone), 16)
        for c in cone:
            self.assertTrue(c["p10"] <= c["p25"] <= c["p50"] <= c["p75"] <= c["p90"])

    def test_no_look_ahead(self):
        """Signals found on a cut of history are the same signals found on the full
        history (only their outcomes can be unfinished)."""
        cut = self.cs[:-300]
        early = engine.analyse(cut)["history"]
        full = {s["candle_t"]: s for s in self.a["history"]}
        cut_end = cut[-1]["t"]
        checked = 0
        for s in early:
            if s["candle_t"] in full and s["outcome"]["result"] != "open":
                f = full[s["candle_t"]]
                for k in ("side", "kind", "entry", "stop", "tp2"):
                    self.assertEqual(s[k], f[k])
                checked += 1
        self.assertGreater(checked, 0)
        self.assertTrue(all(s["candle_t"] <= cut_end for s in early))

    def test_projection_is_repeatable(self):
        p1 = engine.project(self.cs, seed=19, paths=500)
        p2 = engine.project(self.cs, seed=19, paths=500)
        self.assertEqual(p1, p2)

    def test_levels_are_merged(self):
        prices = [lv["price"] for lv in self.a["levels"]]
        self.assertEqual(len(prices), len(set(prices)))


class Sizing(unittest.TestCase):
    def test_lots_from_risk(self):
        z = engine.size(5000, 1, 5.86)          # $50 risk, $586 per lot at the stop
        self.assertEqual(z["lots"], 0.08)
        self.assertAlmostEqual(z["loss_at_stop"], 46.88)

    def test_stop_too_wide(self):
        z = engine.size(500, 0.5, 20)          # $2.5 risk, $20 per 0.01 lot
        self.assertEqual(z["lots"], 0.0)
        self.assertIn("too wide", z["note"])


class CSVs(unittest.TestCase):
    def test_mt5_export_tab_with_offset(self):
        text = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                "2026.10.26\t09:00:00\t2400.10\t2401.00\t2399.00\t2400.50\t120\t0\t20\n"
                "2026.10.26\t09:05:00\t2400.50\t2402.00\t2400.00\t2401.80\t90\t0\t20\n"
                "2026.10.26\t09:10:00\t2401.80\t2403.00\t2401.00\t2402.20\t80\t0\t20\n")
        cs, step = feeds.parse_csv(text, utc_offset_hours=2)
        self.assertEqual(step, 300)
        self.assertEqual(cs[0]["t"], ts("2026-10-26 07:00"))
        m15 = feeds.to_m15(cs, step)
        self.assertEqual(len(m15), 1)
        self.assertEqual((m15[0]["o"], m15[0]["h"], m15[0]["l"], m15[0]["c"]), (2400.10, 2403.0, 2399.0, 2402.2))

    def test_tradingview_unix_times(self):
        t = ts("2026-10-26 07:00")
        text = "time,open,high,low,close,Volume\n%d,1,2,0.5,1.5,10\n%d,1.5,2,1,1.8,11\n" % (t, t + 900)
        cs, step = feeds.parse_csv(text)
        self.assertEqual((len(cs), step, cs[0]["t"]), (2, 900, t))

    def test_refuses_h1_files_and_junk(self):
        t = ts("2026-10-26 07:00")
        text = "time,open,high,low,close\n%d,1,2,0.5,1.5\n%d,1.5,2,1,1.8\n" % (t, t + 3600)
        cs, step = feeds.parse_csv(text)
        with self.assertRaises(ValueError):
            feeds.to_m15(cs, step)
        with self.assertRaises(ValueError):
            feeds.parse_csv("a,b,c\n1,2,3\n")


class Feeds(unittest.TestCase):
    def test_parse_yahoo(self):
        doc = {"chart": {"result": [{"timestamp": [900, 1800, 2700],
                                     "indicators": {"quote": [{"open": [1, None, 3], "high": [2, 2, 4],
                                                               "low": [0.5, 1, 2], "close": [1.5, 2, 3.5],
                                                               "volume": [5, 6, 7]}]}}]}}
        cs = feeds.parse_yahoo(json.dumps(doc))
        self.assertEqual([c["t"] for c in cs], [900, 2700])

    def test_parse_binance(self):
        rows = [[1793000700000, "2400.1", "2401", "2399", "2400.5", "12.3", 0, 0, 0, 0, 0, 0]]
        self.assertEqual(feeds.parse_binance(rows)[0]["t"], 1793000700)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = server.Store(self.dir)
        self.sent = []
        self._tg = server.telegram
        server.telegram = lambda tok, chat, text: self.sent.append(text)
        self.store.settings.update(telegram_token="t", telegram_chat_id="c")

    def tearDown(self):
        server.telegram = self._tg
        shutil.rmtree(self.dir)

    def test_signal_alert_once_then_outcome(self):
        sig = {"t": NOW, "candle_t": NOW - 900, "side": "buy", "kind": "Breakout", "session": "London",
               "entry": 2400.0, "stop": 2395.0, "tp1": 2405.0, "tp2": 2410.0, "risk": 5.0,
               "outcome": {"result": "open", "r": None, "tp1": False}}
        a = {"ok": True, "last_t": NOW, "history": [sig], "price": 2400.0, "bias": {"dir": "bullish"},
             "clock": {"next_t": None}}
        server.raise_alerts(self.store, a)
        server.raise_alerts(self.store, a)
        self.assertEqual([x["kind"] for x in self.store.alerts], ["signal"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("BUY XAUUSD at 2400.00", self.sent[0])
        sig["outcome"] = {"result": "tp2", "r": 2.0, "tp1": True}
        server.raise_alerts(self.store, a)
        self.assertEqual([x["kind"] for x in self.store.alerts], ["signal", "tp1", "tp2"])
        # persisted: a new store sees the same alerts and does not repeat them
        s2 = server.Store(self.dir)
        self.assertEqual(len(s2.alerts), 3)

    def test_quiet_hours_hold_telegram(self):
        self.store.settings["quiet"] = ["00:00", "23:59"]
        a = server.add_alert(self.store, "test", "T", "B")
        self.assertEqual(a["sent"], "quiet hours")
        self.assertEqual(self.sent, [])


class HTTP(unittest.TestCase):
    def test_state_upload_and_pin(self):
        d = tempfile.mkdtemp()
        try:
            st = server.Store(d)
            st.force_demo = True
            server.refresh(st)
            server.App.store, server.App.pin = st, "4321"
            srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.App)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            self.assertIn(b"OBSIDIAN Gold", urllib.request.urlopen(base + "/").read())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/state")
            self.assertEqual(e.exception.code, 401)
            r = urllib.request.urlopen(urllib.request.Request(base + "/api/login", data=b'{"pin":"4321"}'))
            cookie = r.headers["Set-Cookie"].split(";")[0]
            req = urllib.request.Request(base + "/api/state?tf=H1", headers={"Cookie": cookie})
            doc = json.loads(urllib.request.urlopen(req).read())
            self.assertTrue(doc["analysis"]["ok"])
            self.assertEqual(doc["chart"]["tf"], "H1")
            self.assertEqual(doc["settings"]["telegram_token"], "")
            srv.shutdown()
            srv.server_close()
        finally:
            server.App.pin = None
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
