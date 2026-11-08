"""Storage: candles, forecasts, signals (the results log), settings and health.

SQLite by default (zero setup). PostgreSQL, with TimescaleDB if installed, when
``FOREX_DB_URL`` starts with ``postgresql://`` (needs ``psycopg``).

Table ``candles``: pair, timeframe, open_time, open, high, low, close,
tick_volume, spread, keyed on (pair, timeframe, open_time).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .candles import Candle

log = logging.getLogger(__name__)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS candles (
        pair TEXT NOT NULL, timeframe TEXT NOT NULL, open_time BIGINT NOT NULL,
        open DOUBLE PRECISION NOT NULL, high DOUBLE PRECISION NOT NULL,
        low DOUBLE PRECISION NOT NULL, close DOUBLE PRECISION NOT NULL,
        tick_volume BIGINT NOT NULL DEFAULT 0, spread DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (pair, timeframe, open_time))""",
    """CREATE TABLE IF NOT EXISTS forecasts (
        id TEXT PRIMARY KEY, pair TEXT NOT NULL, timeframe TEXT NOT NULL,
        base_time BIGINT NOT NULL, created_at BIGINT NOT NULL, model TEXT NOT NULL,
        payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS forecasts_lookup ON forecasts (pair, timeframe, base_time)",
    """CREATE TABLE IF NOT EXISTS forecast_cache (
        key TEXT PRIMARY KEY, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY, pair TEXT NOT NULL, side TEXT, setup TEXT NOT NULL,
        session TEXT, trading_day TEXT, status TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        outcome TEXT, outcome_r DOUBLE PRECISION, user_action TEXT,
        payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS signals_status ON signals (status, created_at)",
    "CREATE INDEX IF NOT EXISTS signals_day ON signals (trading_day, pair)",
    """CREATE TABLE IF NOT EXISTS settings (
        user_id TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_events (
        ts BIGINT NOT NULL, service TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS health_ts ON health_events (ts)",
]

ACTIVE = ("waiting", "triggered")


class Store:
    def __init__(self, url: str):
        self.url = url
        self.lock = threading.RLock()
        if url.startswith("sqlite:///") or url == "sqlite://":
            path = url[len("sqlite:///"):] if url != "sqlite://" else ":memory:"
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.kind = "sqlite"
            self.conn = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        elif url.startswith(("postgresql://", "postgres://")):
            import psycopg  # optional dependency
            self.kind = "postgres"
            self.conn = psycopg.connect(url, autocommit=True)
        else:
            raise ValueError("unsupported FOREX_DB_URL %r (sqlite:///path or postgresql://...)" % url)
        self._init()

    # ------------------------------------------------------------------ plumbing
    def _sql(self, q: str) -> str:
        return q.replace("?", "%s") if self.kind == "postgres" else q

    def execute(self, q: str, args=()):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self._sql(q), args)
            return cur

    def executemany(self, q: str, rows):
        with self.lock:
            cur = self.conn.cursor()
            if self.kind == "sqlite":
                cur.execute("BEGIN")
                try:
                    cur.executemany(q, rows)
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
            else:
                cur.executemany(self._sql(q), rows)
            return cur

    def query(self, q: str, args=()) -> list:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self._sql(q), args)
            return cur.fetchall()

    def _init(self):
        for s in SCHEMA:
            self.execute(s)
        if self.kind == "postgres":
            try:
                self.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                self.execute("SELECT create_hypertable('candles', 'open_time', chunk_time_interval => 604800, "
                             "if_not_exists => TRUE, migrate_data => TRUE)")
            except Exception as e:  # plain PostgreSQL is fine to start
                log.info("TimescaleDB not used (%s); plain PostgreSQL tables", e)

    def close(self):
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ candles
    def upsert_candles(self, candles: list):
        if not candles:
            return
        rows = [(c.pair, c.tf, c.open_time, c.open, c.high, c.low, c.close, c.tick_volume, c.spread)
                for c in candles]
        self.executemany(
            "INSERT INTO candles (pair, timeframe, open_time, open, high, low, close, tick_volume, spread) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (pair, timeframe, open_time) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "tick_volume=excluded.tick_volume, spread=excluded.spread", rows)

    def get_candles(self, pair: str, tf: str, limit: int | None = 500, end: int | None = None,
                    start: int | None = None) -> list:
        """Candles in time order. ``end`` is exclusive on open_time (candles that opened before it)."""
        q = "SELECT open_time, open, high, low, close, tick_volume, spread FROM candles WHERE pair=? AND timeframe=?"
        args = [pair, tf]
        if start is not None:
            q += " AND open_time >= ?"
            args.append(int(start))
        if end is not None:
            q += " AND open_time < ?"
            args.append(int(end))
        if limit:
            q += " ORDER BY open_time DESC LIMIT ?"
            args.append(int(limit))
            rows = self.query(q, args)[::-1]
        else:
            q += " ORDER BY open_time"
            rows = self.query(q, args)
        return [Candle(pair, tf, int(r[0]), r[1], r[2], r[3], r[4], int(r[5]), r[6]) for r in rows]

    def last_candle_time(self, pair: str, tf: str) -> int | None:
        r = self.query("SELECT MAX(open_time) FROM candles WHERE pair=? AND timeframe=?", (pair, tf))
        return int(r[0][0]) if r and r[0][0] is not None else None

    def first_candle_time(self, pair: str, tf: str) -> int | None:
        r = self.query("SELECT MIN(open_time) FROM candles WHERE pair=? AND timeframe=?", (pair, tf))
        return int(r[0][0]) if r and r[0][0] is not None else None

    def candle_count(self, pair: str, tf: str) -> int:
        return int(self.query("SELECT COUNT(*) FROM candles WHERE pair=? AND timeframe=?", (pair, tf))[0][0])

    # ------------------------------------------------------------------ forecasts
    def save_forecast(self, f: dict):
        payload = json.dumps(f, separators=(",", ":"))
        self.execute(
            "INSERT INTO forecasts (id, pair, timeframe, base_time, created_at, model, payload) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at",
            (f["id"], f["pair"], f["tf"], f["base_time"], f["created_at"], f["model"], payload))

    def latest_forecast(self, pair: str, tf: str) -> dict | None:
        r = self.query("SELECT payload FROM forecasts WHERE pair=? AND timeframe=? "
                       "ORDER BY base_time DESC, created_at DESC LIMIT 1", (pair, tf))
        return json.loads(r[0][0]) if r else None

    def forecasts_between(self, pair: str, tf: str, start: int, end: int) -> list:
        r = self.query("SELECT payload FROM forecasts WHERE pair=? AND timeframe=? AND base_time>=? AND base_time<? "
                       "ORDER BY base_time", (pair, tf, start, end))
        return [json.loads(x[0]) for x in r]

    def prune_forecasts(self, keep_days: int = 30):
        cutoff = int(time.time()) - keep_days * 86400
        self.execute("DELETE FROM forecasts WHERE created_at < ?", (cutoff,))

    def cache_get(self, key: str) -> dict | None:
        r = self.query("SELECT payload FROM forecast_cache WHERE key=?", (key,))
        return json.loads(r[0][0]) if r else None

    def cache_put(self, key: str, payload: dict):
        self.execute("INSERT INTO forecast_cache (key, payload) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                     "payload=excluded.payload", (key, json.dumps(payload, separators=(",", ":"))))

    # ------------------------------------------------------------------ signals (results log)
    def save_signal(self, s: dict):
        now = int(time.time())
        self.execute(
            "INSERT INTO signals (id, pair, side, setup, session, trading_day, status, shown, created_at, updated_at, "
            "outcome, outcome_r, user_action, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET side=excluded.side, status=excluded.status, shown=excluded.shown, "
            "updated_at=excluded.updated_at, outcome=excluded.outcome, outcome_r=excluded.outcome_r, "
            "user_action=excluded.user_action, payload=excluded.payload",
            (s["id"], s["pair"], s.get("side"), s["setup"], s.get("session"), s.get("trading_day"), s["status"],
             1 if s.get("shown") else 0, s.get("created_ts", now), now, s.get("outcome"), s.get("outcome_r"),
             s.get("user_action"), json.dumps(s, separators=(",", ":"), default=str)))

    def get_signal(self, sid: str) -> dict | None:
        r = self.query("SELECT payload FROM signals WHERE id=?", (sid,))
        return json.loads(r[0][0]) if r else None

    def get_signals(self, status: str | None = None, pair: str | None = None, shown: bool | None = None,
                    limit: int = 200, trading_day: str | None = None, since: int | None = None) -> list:
        q, args = "SELECT payload FROM signals WHERE 1=1", []
        if status == "active":
            q += " AND status IN (?, ?)"
            args += list(ACTIVE)
        elif status == "closed":
            q += " AND status IN (?, ?, ?)"
            args += ["closed", "expired", "filtered"]
        elif status:
            q += " AND status=?"
            args.append(status)
        if pair:
            q += " AND pair=?"
            args.append(pair)
        if shown is not None:
            q += " AND shown=?"
            args.append(1 if shown else 0)
        if trading_day:
            q += " AND trading_day=?"
            args.append(trading_day)
        if since is not None:
            q += " AND created_at>=?"
            args.append(int(since))
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        return [json.loads(r[0]) for r in self.query(q, args)]

    # ------------------------------------------------------------------ settings
    def get_settings(self, user_id: str = "default") -> dict | None:
        r = self.query("SELECT value FROM settings WHERE user_id=?", (user_id,))
        return json.loads(r[0][0]) if r else None

    def put_settings(self, value: dict, user_id: str = "default"):
        self.execute("INSERT INTO settings (user_id, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (user_id) "
                     "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (user_id, json.dumps(value), int(time.time())))

    # ------------------------------------------------------------------ health
    def log_health(self, service: str, kind: str, detail=None):
        self.execute("INSERT INTO health_events (ts, service, kind, detail) VALUES (?, ?, ?, ?)",
                     (int(time.time()), service, kind, json.dumps(detail) if detail is not None else None))

    def health_events(self, since: int, kind: str | None = None) -> list:
        q, args = "SELECT ts, service, kind, detail FROM health_events WHERE ts>=?", [since]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY ts"
        return [{"ts": r[0], "service": r[1], "kind": r[2], "detail": json.loads(r[3]) if r[3] else None}
                for r in self.query(q, args)]
