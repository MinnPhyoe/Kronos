"""OBSIDIAN Gold: the private web app.

    python3 obsidian_gold/server.py                 # http://127.0.0.1:8750
    python3 obsidian_gold/server.py --demo          # made-up candles, to look around
    OBSIDIAN_PIN=1234 python3 obsidian_gold/server.py --host 0.0.0.0   # reach it from your phone

Standard library only. It fetches XAUUSD candles every minute, runs the Gold
Pulse analysis (engine.py), keeps a list of alerts, and sends each new alert
to Telegram if you set a bot token and chat id in Settings. The page also
shows a browser notification while it is open.

Analysis only. It never connects to a broker and never places a trade.
"""

import argparse
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

import engine  # noqa: E402
import feeds  # noqa: E402

DATA = os.environ.get("OBSIDIAN_DATA", os.path.join(HERE, "data"))
STATIC = os.path.join(HERE, "static")

SETTINGS_DEFAULT = {
    "source": "yahoo",          # yahoo | binance | csv | demo
    "symbol": "GC=F",           # yahoo: GC=F (futures) or XAUUSD=X (spot); binance: PAXGUSDT
    "poll_seconds": 60,
    "balance": 5000.0, "currency": "USD", "risk_pct": 1.0,
    "max_daily_loss_pct": 3.0,
    "london": ["07:00", "11:00"], "newyork": ["12:00", "16:00"], "asia": ["00:00", "06:00"],
    "tp1_r": 1.0, "tp2_r": 2.0,
    "telegram_token": "", "telegram_chat_id": "",
    "notify": {"signal": True, "tp1": True, "tp2": True, "stop": True, "timeout": True,
               "session": True, "near_entry": False},
    "quiet": ["", ""],          # UTC "22:00","06:00" = nothing to Telegram in these hours
    "csv_offset_hours": 0.0,
}


def now():
    return int(time.time())


class Store:
    """Settings, alerts and the candle cache, kept as JSON files in data/."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.lock = threading.RLock()
        self.settings = dict(SETTINGS_DEFAULT, **self._load("settings.json", {}))
        self.settings["notify"] = dict(SETTINGS_DEFAULT["notify"], **self.settings.get("notify", {}))
        st = self._load("state.json", {})
        self.alerts = st.get("alerts", [])
        self.seen = st.get("seen", {})
        self.sessions_sent = st.get("sessions_sent", [])
        self.candles = self._load("candles.json", [])
        self.status = {"ok": bool(self.candles), "source": self.settings["source"],
                       "updated": None, "error": None}
        self.analysis = None
        self.kronos = None
        self.force_demo = False

    def _load(self, name, default):
        try:
            with open(os.path.join(self.root, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def _save(self, name, obj):
        p = os.path.join(self.root, name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, p)

    def save_state(self):
        with self.lock:
            self._save("state.json", {"alerts": self.alerts[-300:], "seen": self.seen,
                                      "sessions_sent": self.sessions_sent[-20:]})

    def save_settings(self):
        with self.lock:
            self._save("settings.json", self.settings)

    def save_candles(self):
        with self.lock:
            self._save("candles.json", self.candles)


# ----------------------------------------------------------- notifications

def telegram(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read().decode())
    if not doc.get("ok"):
        raise RuntimeError(doc.get("description", "Telegram refused the message"))


def in_quiet(s, t):
    q = s.get("quiet") or ["", ""]
    if not q[0] or not q[1]:
        return False
    return engine.in_window(t, q)


def add_alert(store, kind, title, body, key=None, push=True):
    with store.lock:
        if key and any(a.get("key") == key for a in store.alerts):
            return None
        a = {"id": "%d-%s" % (now(), secrets.token_hex(3)), "t": now(), "kind": kind,
             "title": title, "body": body, "key": key, "read": False, "sent": None}
        store.alerts.append(a)
        s = store.settings
    if push and s["notify"].get(kind, True) and s.get("telegram_token") and s.get("telegram_chat_id"):
        if in_quiet(s, a["t"]):
            a["sent"] = "quiet hours"
        else:
            try:
                telegram(s["telegram_token"], s["telegram_chat_id"], "OBSIDIAN Gold\n%s\n%s" % (title, body))
                a["sent"] = "telegram"
            except Exception as e:
                a["sent"] = "failed: %s" % e
    store.save_state()
    return a


def fmt(p):
    return "%.2f" % p


def signal_text(s, size=None):
    side = "BUY" if s["side"] == "buy" else "SELL"
    lines = ["%s XAUUSD at %s (%s, %s)" % (side, fmt(s["entry"]), s["kind"], s["session"]),
             "Stop %s   TP1 %s   TP2 %s" % (fmt(s["stop"]), fmt(s["tp1"]), fmt(s["tp2"])),
             "Risk %s per oz" % fmt(s["risk"])]
    if size and size.get("lots"):
        lines.append("Size %.2f lots, about %s at the stop" % (size["lots"], fmt(size["loss_at_stop"])))
    lines.append("Analysis only, not advice. Check the chart before you act.")
    return "\n".join(lines)


def raise_alerts(store, a):
    """Turn changes in the analysis into alerts, once each."""
    if not a or not a.get("ok"):
        return
    recent_cut = a["last_t"] - 3 * 86400
    for s in a["history"]:
        if s["t"] < recent_cut:
            continue
        sid = "%d-%s" % (s["candle_t"], s["side"])
        prev = store.seen.get(sid)
        o = s["outcome"]
        cur = {"result": o["result"], "tp1": bool(o["tp1"])}
        if prev is None:
            store.seen[sid] = cur
            if o["result"] == "open":
                add_alert(store, "signal", "%s signal: %s at %s" % (
                    s["kind"], "BUY" if s["side"] == "buy" else "SELL", fmt(s["entry"])),
                    signal_text(s, s.get("size")), key="sig-" + sid)
            continue
        if cur["tp1"] and not prev["tp1"]:
            add_alert(store, "tp1", "Target 1 reached at %s" % fmt(s["tp1"]),
                      "Consider moving the stop to entry (%s)." % fmt(s["entry"]), key="tp1-" + sid)
        if cur["result"] != prev["result"]:
            r = cur["result"]
            if r == "tp2":
                add_alert(store, "tp2", "Target 2 reached at %s" % fmt(s["tp2"]), "+%gR. Trade finished." %
                          o["r"], key="tp2-" + sid)
            elif r in ("stop", "tp1_then_stop"):
                add_alert(store, "stop", "Stop hit at %s" % fmt(s["stop"] if r == "stop" else s["entry"]),
                          "%s. Trade finished." % ("-1R" if r == "stop" else "Back to entry after target 1"),
                          key="sl-" + sid)
            elif r == "timeout":
                add_alert(store, "timeout", "Time is up on the %s signal" % s["kind"],
                          "8 hours passed. Close at market: %+gR." % o["r"], key="to-" + sid)
        store.seen[sid] = cur
    # session opens, 5 minutes before
    c = a["clock"]
    if c.get("next_t") and 0 <= c["next_t"] - now() <= 300:
        k = "sess-%d" % c["next_t"]
        if k not in store.sessions_sent:
            store.sessions_sent.append(k)
            add_alert(store, "session", "%s opens in 5 minutes" % c["next"],
                      "Bias: %s. Price %s." % (a["bias"]["dir"], fmt(a["price"])), key=k)
    store.save_state()


# ------------------------------------------------------------------ worker

def refresh(store, force_demo=False):
    s = store.settings
    src = "demo" if (force_demo or store.force_demo) else s["source"]
    try:
        if src == "yahoo":
            cs = feeds.yahoo(s.get("symbol") or "GC=F")
        elif src == "binance":
            cs = feeds.binance(s.get("symbol") or "PAXGUSDT")
        elif src == "demo":
            cs = feeds.demo()
        else:
            cs = None  # csv: keep what was uploaded
        if cs is not None:
            if len(cs) < 50:
                raise RuntimeError("only %d candles came back" % len(cs))
            with store.lock:
                store.candles = cs[-6000:]
            store.save_candles()
        store.status = {"ok": True, "source": src, "updated": now(), "error": None}
    except Exception as e:
        store.status = {"ok": bool(store.candles), "source": src, "updated": store.status.get("updated"),
                        "error": str(e)}
    run_analysis(store)


def run_analysis(store):
    s = store.settings
    with store.lock:
        cs = list(store.candles)
    if not cs:
        store.analysis = {"ok": False, "why": "No candles yet."}
        return
    cfg = {k: s[k] for k in ("london", "newyork", "asia", "tp1_r", "tp2_r")}
    try:
        a = engine.analyse(cs, cfg, now_t=max(now(), cs[-1]["t"] + 900),
                           balance=s.get("balance"), risk_pct=s.get("risk_pct"))
        if a.get("ok"):
            for h in a["history"]:
                if h["outcome"]["result"] == "open":
                    h["size"] = engine.size(s["balance"], s["risk_pct"], h["risk"])
        store.analysis = a
        if store.status.get("source") != "demo":
            raise_alerts(store, a)
    except Exception as e:
        traceback.print_exc()
        store.analysis = {"ok": False, "why": "Analysis failed: %s" % e}


def worker(store, stop, force_demo):
    while not stop.is_set():
        refresh(store, force_demo)
        stop.wait(max(30, int(store.settings.get("poll_seconds") or 60)))


# ------------------------------------------------------------------ kronos

def kronos_forecast(store, bars=16):
    """Optional: the Kronos model's forecast of the next candles. Needs torch
    and the model from Hugging Face (pip install -r requirements.txt)."""
    try:
        root = os.path.dirname(HERE)
        if root not in sys.path:
            sys.path.insert(0, root)
        import pandas as pd
        from model import Kronos, KronosTokenizer, KronosPredictor
    except Exception as e:
        return {"ok": False, "why": "Kronos is not installed here (%s). Run: pip install -r requirements.txt" % e}
    with store.lock:
        cs = list(store.candles)[-400:]
    if len(cs) < 100:
        return {"ok": False, "why": "Need at least 100 candles."}
    if not getattr(kronos_forecast, "predictor", None):
        tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        mdl = Kronos.from_pretrained("NeoQuasar/Kronos-small")
        kronos_forecast.predictor = KronosPredictor(mdl, tok, max_context=512)
    df = pd.DataFrame([{"open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"], "volume": c.get("v", 0)}
                       for c in cs])
    xt = pd.Series(pd.to_datetime([c["t"] for c in cs], unit="s"))
    yt = pd.Series(pd.to_datetime([cs[-1]["t"] + 900 * (k + 1) for k in range(bars)], unit="s"))
    pred = kronos_forecast.predictor.predict(df=df, x_timestamp=xt, y_timestamp=yt, pred_len=bars,
                                             T=1.0, top_p=0.9, sample_count=5, verbose=False)
    out = [{"t": cs[-1]["t"] + 900 * (k + 1), "o": round(float(r.open), 2), "h": round(float(r.high), 2),
            "l": round(float(r.low), 2), "c": round(float(r.close), 2)} for k, r in enumerate(pred.itertuples())]
    return {"ok": True, "model": "Kronos-small", "from": cs[-1]["c"], "candles": out, "made": now()}


# -------------------------------------------------------------------- http

def public_settings(s):
    p = dict(s)
    p["telegram_token"] = ("set" if s.get("telegram_token") else "")
    return p


def candles_for(store, tf, n=320):
    with store.lock:
        cs = list(store.candles)
    sec = engine.TF_SECONDS.get(tf, 900)
    sec = max(sec, 900)
    if sec > 900:
        cs = engine.resample(cs, sec)
    cs = cs[-(n + 200):]
    closes = [c["c"] for c in cs]
    e20, e50 = engine.ema(closes, 20), engine.ema(closes, 50)
    cut = max(0, len(cs) - n)
    return {"tf": tf, "candles": cs[cut:], "ema20": e20[cut:], "ema50": e50[cut:]}


class App(BaseHTTPRequestHandler):
    store = None
    pin = None
    secret = secrets.token_hex(16)
    server_version = "OBSIDIAN"

    def log_message(self, fmt_, *args):
        pass

    # auth: one PIN, a signed cookie
    def _token(self):
        return hmac.new(self.secret.encode(), (self.pin or "").encode(), hashlib.sha256).hexdigest()

    def _authed(self):
        if not self.pin:
            return True
        ck = self.headers.get("Cookie") or ""
        return ("obs=%s" % self._token()) in ck

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 30 * 1024 * 1024:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, {"error": "pin"})
        st = self.store
        if u.path == "/api/state":
            tf = (q.get("tf") or ["M15"])[0]
            with st.lock:
                alerts = list(st.alerts[-100:])
            return self._send(200, {
                "now": now(), "status": st.status, "settings": public_settings(st.settings),
                "analysis": st.analysis, "chart": candles_for(st, tf), "alerts": alerts[::-1],
                "kronos": st.kronos, "pin": bool(self.pin)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        st = self.store
        if u.path == "/api/login":
            try:
                pin = json.loads(self._body() or b"{}").get("pin", "")
            except ValueError:
                pin = ""
            if self.pin and hmac.compare_digest(str(pin), self.pin):
                return self._send(200, {"ok": True}, extra={
                    "Set-Cookie": "obs=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % self._token()})
            return self._send(401, {"ok": False})
        if not self._authed():
            return self._send(401, {"error": "pin"})
        try:
            if u.path == "/api/settings":
                patch = json.loads(self._body() or b"{}")
                with st.lock:
                    for k, v in patch.items():
                        if k not in SETTINGS_DEFAULT:
                            continue
                        if k == "telegram_token" and v == "set":
                            continue
                        if k == "notify":
                            st.settings["notify"] = dict(st.settings["notify"], **v)
                        else:
                            st.settings[k] = v
                st.save_settings()
                threading.Thread(target=refresh, args=(st,), daemon=True).start()
                return self._send(200, {"ok": True, "settings": public_settings(st.settings)})
            if u.path == "/api/refresh":
                refresh(st)
                return self._send(200, {"ok": True, "status": st.status})
            if u.path == "/api/upload":
                text = self._body().decode("utf-8", "replace")
                off = float((q.get("offset") or [st.settings.get("csv_offset_hours", 0)])[0] or 0)
                cs, step = feeds.parse_csv(text, off)
                cs = feeds.to_m15(cs, step)
                with st.lock:
                    st.candles = cs[-20000:]
                    st.settings["source"] = "csv"
                st.save_candles()
                st.save_settings()
                st.status = {"ok": True, "source": "csv", "updated": now(), "error": None}
                run_analysis(st)
                return self._send(200, {"ok": True, "candles": len(cs),
                                        "from": cs[0]["t"], "to": cs[-1]["t"]})
            if u.path == "/api/test-alert":
                a = add_alert(st, "test", "Test alert", "If you can read this, alerts reach you.")
                return self._send(200, {"ok": True, "alert": a})
            if u.path == "/api/alerts/read":
                with st.lock:
                    for a in st.alerts:
                        a["read"] = True
                st.save_state()
                return self._send(200, {"ok": True})
            if u.path == "/api/alerts/clear":
                with st.lock:
                    st.alerts = []
                st.save_state()
                return self._send(200, {"ok": True})
            if u.path == "/api/kronos":
                st.kronos = kronos_forecast(st)
                return self._send(200, st.kronos)
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": str(e)})
        return self._send(404, {"error": "not found"})


def main(argv=None):
    ap = argparse.ArgumentParser(description="OBSIDIAN Gold, your private XAUUSD desk")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8750)))
    ap.add_argument("--demo", action="store_true", help="made-up candles, no network")
    a = ap.parse_args(argv)
    pin = os.environ.get("OBSIDIAN_PIN") or None
    if a.host not in ("127.0.0.1", "localhost") and not pin:
        sys.exit("Set OBSIDIAN_PIN before opening the app beyond this computer, e.g. OBSIDIAN_PIN=482913")
    store = Store(DATA)
    store.force_demo = a.demo
    App.store, App.pin = store, pin
    stop = threading.Event()
    threading.Thread(target=worker, args=(store, stop, a.demo), daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), App)
    print("OBSIDIAN Gold on http://%s:%d %s" % (a.host, a.port, "(demo data)" if a.demo else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
