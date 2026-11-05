"""Offline tests for OBSIDIAN Gold. They need your day-trader-skill clone
(next to Kronos, or DAY_TRADER_PATH) for its tools and its TSLA fixture.

    python3 -m unittest discover -s obsidian_gold/tests -v
"""

import datetime
import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import desk  # noqa: E402
import server  # noqa: E402

try:
    T = desk.Tools()
except desk.DeskError:
    T = None

REEL_NOW = datetime.datetime(2026, 10, 19, 14, 15)   # 10:15 AM ET, the README's trade
ACCOUNT = {"balance": 5000, "risk_pct": 1, "leverage": 1}


def tsla():
    path = os.path.join(T.root, "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")
    return gzip.open(path).read().decode()


@unittest.skipIf(T is None, "day-trader-skill not found")
class SameNumbersAsDayTrader(unittest.TestCase):
    """The day-trader README's TSLA trade, run through this app: the same entry,
    stop, target, size and history the day-trader tools print."""

    @classmethod
    def setUpClass(cls):
        cls.a = desk.analyse(T, tsla(), REEL_NOW, ACCOUNT)

    def test_entry_stop_target(self):
        a = self.a
        self.assertEqual((a["status"], a["side"]), ("triggered", "buy"))
        self.assertEqual(a["range"]["high"], 362.18)
        self.assertEqual(a["range"]["low"], 354.47)
        p = a["plan"]
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk"]), (364.18, 358.32, 370.04, 5.86))
        self.assertEqual(p["entry_time"], "10:15")

    def test_size(self):
        z = self.a["size"]
        self.assertEqual(z["ounces"], 8)        # 50 / 5.86 = 8 shares in the README
        self.assertEqual(z["lots"], 0.08)
        self.assertAlmostEqual(z["loss_at_stop"], 46.88)

    def test_time_machine_history(self):
        h = self.a["odds"]["history"]
        self.assertEqual((h["sessions"], h["triggered"], h["target"], h["stop"], h["close"]), (59, 33, 13, 15, 5))
        o = self.a["odds"]
        self.assertAlmostEqual(o["p_target"] + o["p_stop"] + o["p_neither"], 1.0, places=3)

    def test_sell_is_the_mirror_image(self):
        doc = desk.relabel(tsla())
        a = desk.analyse(T, json.dumps(desk.mirror(doc, 1000)), REEL_NOW, dict(ACCOUNT, leverage=100))
        self.assertEqual(a["side"], "sell")
        p = a["plan"]
        self.assertEqual((p["entry"], p["stop"], p["target"]), (635.82, 641.68, 629.96))
        self.assertEqual(a["size"]["ounces"], 8)
        self.assertEqual(a["odds"]["history"]["triggered"], 33)

    def test_before_the_open_and_while_forming(self):
        a = desk.analyse(T, tsla(), datetime.datetime(2026, 10, 19, 13, 0), ACCOUNT)
        self.assertEqual(a["phase"], "pre-open")
        self.assertIsNone(a["plan"])
        a = desk.analyse(T, tsla(), datetime.datetime(2026, 10, 19, 13, 40), ACCOUNT)
        self.assertEqual(a["phase"], "forming")


@unittest.skipIf(T is None, "day-trader-skill not found")
class Alerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = server.Store(self.dir)
        self.sent = []
        self._tg = server.telegram
        server.telegram = lambda tok, chat, text: self.sent.append(text)
        self.store.settings.update(telegram_token="t", telegram_chat_id="c", balance=5000, risk_pct=1, leverage=1)

    def tearDown(self):
        server.telegram = self._tg
        shutil.rmtree(self.dir)

    def test_range_entry_then_target_once_each(self):
        a = desk.analyse(T, tsla(), REEL_NOW, self.store.settings)
        server.events(self.store, a, 364.5, REEL_NOW)
        server.events(self.store, a, 364.5, REEL_NOW)
        self.assertEqual([x["kind"] for x in self.store.alerts], ["range", "entry"])
        self.assertIn("Entry 364.18   Stop 358.32   Target 370.04", self.sent[1])
        server.events(self.store, a, 370.10, REEL_NOW + datetime.timedelta(minutes=30))
        self.assertEqual([x["kind"] for x in self.store.alerts], ["range", "entry", "target"])
        server.events(self.store, a, 370.20, REEL_NOW + datetime.timedelta(minutes=35))
        self.assertEqual(len(self.store.alerts), 3)
        self.assertEqual(len(server.Store(self.dir).alerts), 3)   # kept on disk

    def test_stop_and_flat_by(self):
        a = desk.analyse(T, tsla(), REEL_NOW, self.store.settings)
        server.events(self.store, a, 358.0, REEL_NOW)
        server.events(self.store, a, 358.0, datetime.datetime(2026, 10, 19, 19, 56))   # 15:56 ET
        kinds = [x["kind"] for x in self.store.alerts]
        self.assertIn("stop", kinds)
        self.assertIn("flat", kinds)


class Mirror(unittest.TestCase):
    def test_highs_become_lows(self):
        doc = {"chart": {"result": [{"meta": {"symbol": "GC=F", "previousClose": 10}, "timestamp": [0],
                                     "indicators": {"quote": [{"open": [10], "high": [12], "low": [9], "close": [11],
                                                               "volume": [1]}]}}]}}
        m = desk.mirror(doc, 100)
        q = m["chart"]["result"][0]["indicators"]["quote"][0]
        self.assertEqual((q["open"], q["high"], q["low"], q["close"]), ([90], [91], [88], [89]))
        self.assertEqual(m["chart"]["result"][0]["meta"]["previousClose"], 90)

    def test_relabel_and_errors(self):
        self.assertEqual(desk.relabel(desk.demo_text(REEL_NOW, days=3))["chart"]["result"][0]["meta"]["symbol"], "XAUUSD")
        with self.assertRaises(desk.DeskError):
            desk.relabel('{"chart": {"result": null, "error": {"description": "No data found"}}}')


class Kronos(unittest.TestCase):
    def test_forecast_shape_with_a_stub_model(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")

        class Stub:
            def predict(self, df, x_timestamp, y_timestamp, pred_len, **kw):
                last = float(df["close"].iloc[-1])
                rows = [{"open": last, "high": last + 1, "low": last - 1, "close": last + k + 1,
                         "volume": 0, "amount": 0} for k in range(pred_len)]
                return pd.DataFrame(rows, index=y_timestamp)
        k = desk.kronos_forecast(desk.demo_text(REEL_NOW, days=5), predictor=Stub())
        self.assertEqual(len(k["path"]), 12)
        self.assertEqual(k["direction"], "up")
        self.assertAlmostEqual(k["change"], 12.0)
        self.assertEqual(k["path"][0]["t"], k["from_t"] + 300)


@unittest.skipIf(T is None, "day-trader-skill not found")
class HTTP(unittest.TestCase):
    def test_pin_and_state(self):
        d = tempfile.mkdtemp()
        try:
            st = server.Store(d)
            st.demo, st.fixed_now = True, datetime.datetime(2026, 11, 4, 17, 40)
            st.settings["kronos"] = False
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
            ck = r.headers["Set-Cookie"].split(";")[0]
            doc = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers={"Cookie": ck})).read())
            self.assertIsNone(doc["analysis"].get("error"))
            self.assertIn("bars", doc["analysis"]["chart"])
            self.assertEqual(doc["settings"]["telegram_token"], "")
            srv.shutdown()
            srv.server_close()
        finally:
            server.App.pin = None
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
