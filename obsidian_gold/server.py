"""OBSIDIAN Gold: a private XAUUSD page built from your two repos.

    python3 obsidian_gold/server.py            # http://127.0.0.1:8750
    python3 obsidian_gold/server.py --demo     # made-up candles, to look around
    OBSIDIAN_PIN=482913 python3 obsidian_gold/server.py --host 0.0.0.0   # from your phone

Every minute it reads gold's 5-minute bars from Yahoo and runs the
day-trader-skill tools on them (desk.py): the opening-range breakout entry,
the stop and target, the Time Machine odds, the size, and the stop/target/
flat-by alerts. Kronos forecasts the next hour. New events go to Telegram.

Analysis only: it never connects to a broker and never places a trade.
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import desk  # noqa: E402

DATA = os.environ.get("OBSIDIAN_DATA", os.path.join(HERE, "data"))
STATIC = os.path.join(HERE, "static")

SETTINGS = {
    "symbol": "GC=F",            # Yahoo: GC=F gold futures, or XAUUSD=X spot
    "balance": 5000.0, "currency": "USD", "risk_pct": 1.0, "leverage": 100,
    "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "kronos": True,
    "telegram_token": "", "telegram_chat_id": "",
}
NUMBERS = {"balance": float, "risk_pct": float, "leverage": float, "or_minutes": int, "target_r": float}


def now_ts():
    return int(time.time())


class Store:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.lock = threading.RLock()
        self.settings = dict(SETTINGS, **self._read("settings.json", {}))
        st = self._read("state.json", {})
        self.alerts = st.get("alerts", [])
        self.rung = st.get("rung", {})
        self.done = st.get("done", [])
        self.text = None
        self.analysis = None
        self.kronos = None
        self.kronos_busy = False
        self.status = {"ok": False, "source": None, "updated": None, "error": None}
        self.demo = False
        self.fixed_now = None
        self.tools = None
        self.tools_error = None
        try:
            self.tools = desk.Tools()
        except desk.DeskError as e:
            self.tools_error = str(e)

    def _read(self, name, default):
        try:
            with open(os.path.join(self.root, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def _write(self, name, obj):
        p = os.path.join(self.root, name)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(p + ".tmp", p)

    def save(self):
        with self.lock:
            self._write("state.json", {"alerts": self.alerts[-200:], "rung": self.rung, "done": self.done[-200:]})
            self._write("settings.json", self.settings)


# ---------------------------------------------------------------- alerts

def telegram(token, chat_id, text):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read().decode())
    if not doc.get("ok"):
        raise RuntimeError(doc.get("description", "Telegram refused it"))


def notify(store, kind, title, body, key=None):
    """One alert, once per key. Sent to Telegram when it is set up."""
    with store.lock:
        if key:
            if key in store.done:
                return None
            store.done.append(key)
        a = {"id": "%d-%s" % (now_ts(), secrets.token_hex(3)), "t": now_ts(), "kind": kind,
             "title": title, "body": body, "read": False, "sent": None}
        store.alerts.append(a)
        s = store.settings
    if s.get("telegram_token") and s.get("telegram_chat_id") and not store.demo:
        try:
            telegram(s["telegram_token"], s["telegram_chat_id"], "XAUUSD · %s\n%s" % (title, body))
            a["sent"] = "telegram"
        except Exception as e:
            a["sent"] = "failed: %s" % e
    store.save()
    return a


def events(store, a, price, now_utc):
    """Turn the analysis into alerts: the range is set, an entry triggers, then
    trader-alerts' stop / target / flat-by rules."""
    day = a["date"]
    rg = a.get("range")
    if rg and rg["complete"] and a["phase"] != "closed":
        notify(store, "range", "Opening range set: %.2f – %.2f" % (rg["low"], rg["high"]),
               "Buy on a 5-minute close above %.2f, sell on a close below %.2f." % (rg["high"], rg["low"]),
               key="range-" + day)
    p = a.get("plan")
    if p:
        z = a.get("size") or {}
        o = a.get("odds") or {}
        lines = ["Entry %.2f   Stop %.2f   Target %.2f" % (p["entry"], p["stop"], p["target"])]
        if z.get("lots"):
            lines.append("%.2f lots: about %.2f lost at the stop" % (z["lots"], z["loss_at_stop"]))
        elif z.get("refusal"):
            lines.append("Size: %s" % z["refusal"])
        if o:
            lines.append("Time Machine: target first %.0f%%, stop first %.0f%%" % (100 * o["p_target"], 100 * o["p_stop"]))
        k = store.kronos
        if k and k.get("ok"):
            agree = (k["direction"] == "up") == (p["side"] == "buy")
            lines.append("Kronos: %s %.2f next hour (%s)" % (k["direction"], abs(k["change"]), "agrees" if agree else "disagrees"))
        lines.append("Flat by %s ET. Not advice." % p["flat_by"])
        notify(store, "entry", "%s XAUUSD at %.2f" % (p["side"].upper(), p["entry"]), "\n".join(lines),
               key="entry-" + day)
        rings, store.rung = desk.alert_rings(store.tools, a, price, store.rung, now_utc)
        for kind, title, body in rings:
            notify(store, kind, title, body, key="%s-%s" % (kind, day))


# ---------------------------------------------------------------- worker

def refresh(store):
    if not store.tools:
        store.analysis = {"error": store.tools_error}
        return
    try:
        text = desk.demo_text(store.fixed_now) if store.demo else store.tools.fetch(store.settings["symbol"])
        store.text = text
        store.status = {"ok": True, "source": "demo" if store.demo else "yahoo", "updated": now_ts(), "error": None}
    except Exception as e:
        store.status = dict(store.status, error=str(e))
        if not store.text:
            store.analysis = {"error": "Could not read prices: %s" % e}
            return
    now_utc = store.fixed_now or desk.utc_now()
    try:
        a = desk.analyse(store.tools, store.text, now_utc, store.settings)
        a["chart"] = chart(store.text, a, store.tools)
        store.analysis = a
        price = a["chart"]["bars"][-1]["c"] if a["chart"]["bars"] else None
        events(store, a, price, now_utc)
    except Exception as e:
        traceback.print_exc()
        store.analysis = {"error": "Analysis failed: %s" % e}
    if store.settings.get("kronos"):
        start_kronos(store)


def start_kronos(store):
    if store.kronos_busy or not store.text:
        return
    doc = desk.relabel(store.text)
    bars = desk.all_bars(doc)
    if not bars or (store.kronos and store.kronos.get("from_t") == bars[-1]["t"]):
        return
    store.kronos_busy = True

    def run():
        try:
            k = desk.kronos_forecast(doc)
            k["ok"] = True
        except Exception as e:
            k = {"ok": False, "why": "Kronos could not run: %s. Install it with pip install -r requirements.txt." % e,
                 "from_t": bars[-1]["t"]}
        store.kronos = k
        store.kronos_busy = False
    threading.Thread(target=run, daemon=True).start()


def chart(text, a, tools):
    """The last 8 hours of 5-minute bars, plus where today's range and plan sit in time."""
    bars = desk.all_bars(desk.relabel(text))[-96:]
    E = tools.entry
    out = {"bars": bars}
    try:
        day = datetime.date.fromisoformat(a["date"])

        def at(hhmm):
            h, m = hhmm.split(":")
            return desk.ts_of(E.eastern_to_utc(datetime.datetime.combine(day, datetime.time(int(h), int(m)))))
        out["open_t"] = at("09:30")
        out["range_end_t"] = out["open_t"] + 60 * int((a.get("range") or {}).get("minutes", 15))
        if a.get("plan"):
            out["entry_t"] = at(a["plan"]["entry_time"])
            out["flat_t"] = at(a["plan"]["flat_by"])
    except (ValueError, TypeError, KeyError):
        pass
    return out


def worker(store, stop):
    while not stop.is_set():
        refresh(store)
        stop.wait(60)


# ------------------------------------------------------------------ http

def public_settings(s):
    return dict(s, telegram_token="set" if s.get("telegram_token") else "")


class App(BaseHTTPRequestHandler):
    store = None
    pin = None
    secret = secrets.token_hex(16)

    def log_message(self, *a):
        pass

    def _cookie(self):
        return hmac.new(self.secret.encode(), (self.pin or "").encode(), hashlib.sha256).hexdigest()

    def _ok(self):
        return not self.pin or ("obs=%s" % self._cookie()) in (self.headers.get("Cookie") or "")

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100000:
            raise ValueError("too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ValueError("not JSON")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if not self._ok():
            return self._send(401, {"error": "pin"})
        st = self.store
        if path == "/api/state":
            with st.lock:
                alerts = st.alerts[-60:][::-1]
            return self._send(200, {"now": now_ts(), "status": st.status, "settings": public_settings(st.settings),
                                    "analysis": st.analysis, "kronos": st.kronos, "kronos_busy": st.kronos_busy,
                                    "alerts": alerts, "demo": st.demo,
                                    "now_view": desk.ts_of(st.fixed_now) if st.fixed_now else now_ts(),
                                    "day_trader": st.tools.root if st.tools else None})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        st = self.store
        try:
            if path == "/api/login":
                pin = str(self._json().get("pin", ""))
                if self.pin and hmac.compare_digest(pin, self.pin):
                    return self._send(200, {"ok": True}, headers={
                        "Set-Cookie": "obs=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % self._cookie()})
                return self._send(401, {"ok": False})
            if not self._ok():
                return self._send(401, {"error": "pin"})
            if path == "/api/settings":
                patch = self._json()
                with st.lock:
                    for k, v in patch.items():
                        if k not in SETTINGS or (k == "telegram_token" and v == "set"):
                            continue
                        st.settings[k] = NUMBERS[k](v) if k in NUMBERS else v
                st.save()
                threading.Thread(target=refresh, args=(st,), daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/refresh":
                refresh(st)
                return self._send(200, {"ok": True})
            if path == "/api/test-alert":
                return self._send(200, {"ok": True, "alert": notify(st, "test", "Test alert",
                                                                    "If you can read this, alerts reach you.")})
            if path == "/api/alerts/read":
                with st.lock:
                    for a in st.alerts:
                        a["read"] = True
                st.save()
                return self._send(200, {"ok": True})
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        return self._send(404, {"error": "not found"})


def main(argv=None):
    ap = argparse.ArgumentParser(description="OBSIDIAN Gold")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8750)))
    ap.add_argument("--demo", action="store_true", help="made-up candles, no network")
    ap.add_argument("--demo-at", help='with --demo: pretend it is this UTC time, e.g. "2026-11-04 16:30"')
    a = ap.parse_args(argv)
    pin = os.environ.get("OBSIDIAN_PIN") or None
    if a.host not in ("127.0.0.1", "localhost") and not pin:
        sys.exit("Set OBSIDIAN_PIN before opening the page beyond this computer.")
    store = Store(DATA)
    store.demo = a.demo or bool(a.demo_at)
    if a.demo_at:
        store.fixed_now = datetime.datetime.fromisoformat(a.demo_at)
    print("Using day-trader-skill at", store.tools.root if store.tools else "(missing) " + store.tools_error)
    App.store, App.pin = store, pin
    stop = threading.Event()
    threading.Thread(target=worker, args=(store, stop), daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), App)
    print("OBSIDIAN Gold on http://%s:%d%s" % (a.host, a.port, " (demo)" if a.demo else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
