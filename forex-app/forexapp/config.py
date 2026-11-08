"""Service configuration (environment) and per-user trading settings (database).

Two kinds of settings live here:

* ``Config``: how the backend runs (database, provider, model, signal rules).
  Read once from environment variables (``FOREX_*``), see ``.env.example``.
* ``UserSettings``: what the trader controls in the app (account size, max
  loss, daily loss limit, pairs, sessions). Stored in the database and edited
  from the Settings screen.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # forex-app/
KRONOS_REPO_DEFAULT = APP_DIR.parent                       # the Kronos repo that holds forex-app/

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}
HIGHER_TIMEFRAMES = ("5m", "15m", "1h")


def load_dotenv(path) -> None:
    """Read KEY=value lines from a .env file into the environment (values already set win)."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def norm_tf(tf: str) -> str:
    """Accept '1H', '5M', '60m' and return the canonical key ('1h', '5m')."""
    t = (tf or "").strip().lower()
    if t == "60m":
        t = "1h"
    if len(t) >= 2 and t[0] in "mhd" and t[1:].isdigit():      # MetaTrader / OBSIDIAN names: M15, H4, D1
        t = t[1:] + t[0]
    if t not in TIMEFRAMES:
        raise ValueError("unknown timeframe %r (use one of %s)" % (tf, ", ".join(TIMEFRAMES)))
    return t


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    return int(_env(name, default))


def _env_float(name, default):
    return float(_env(name, default))


def _env_list(name, default):
    v = _env(name)
    if v is None:
        return list(default)
    return [x.strip().upper() for x in v.split(",") if x.strip()]


@dataclass
class Config:
    # storage and messaging
    db_url: str = "sqlite:///" + str(APP_DIR / "data" / "forex.db")
    redis_url: str | None = None

    # data provider: yahoo (free, no key) | oanda | eodhd | tradermade | simulated
    provider: str = "simulated"
    pairs: list = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])
    price_basis: str = "bid"                 # candles are built from bid prices; the spread is stored separately
    oanda_token: str | None = None
    oanda_account: str | None = None
    oanda_env: str = "practice"              # practice | live
    eodhd_key: str | None = None
    tradermade_key: str | None = None
    backfill_days: int = 30                  # on first start / reconnect, backfill at most this many days of 1m candles
    poll_seconds: float = 5.0                # yahoo: how often to read the latest prices

    # simulated provider (demo / development)
    sim_speed: float = 1.0                   # 60 = one market minute per real second
    sim_start: str | None = None             # e.g. "2026-11-09T06:45:00Z"; default: now
    sim_history_days: int = 20
    sim_seed: int = 7

    # forecasting model
    model_backend: str = "kronos"            # kronos | bootstrap (statistical demo fallback, not Kronos)
    kronos_repo: str = str(KRONOS_REPO_DEFAULT)
    kronos_model: str = "NeoQuasar/Kronos-small"
    kronos_tokenizer: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_checkpoint: str | None = None     # fine-tuned predictor dir (Phase 6); falls back to kronos_model
    kronos_tokenizer_checkpoint: str | None = None
    kronos_device: str | None = None         # cuda:0 | cpu | mps; default: auto
    max_context: int = 512
    lookback: int = 480                      # 400-512 closed candles
    horizon: int = 24                        # candles ahead (2 hours on 5m)
    paths: int = 30                          # forecast paths, kept separately (never averaged)
    temperature: float = 1.0
    top_p: float = 0.9
    chart_forecasts: bool = True             # also forecast every pair on each 5m close for the chart band
    model_timeout_s: float = 120.0

    # signal rules (Phase 3-4)
    sessions: list = field(default_factory=lambda: ["london", "newyork", "tokyo"])
    setup_expiry_minutes: int = 90
    stop_rule: str = "or_mid"                # or_mid (tighter) | or_far (opposite side of the range)
    min_stop_atr: float = 0.5                # stop at least 0.5 x ATR(14) on 5m
    stop_buffer_spreads: float = 1.0         # stop sits this many spreads beyond the structural level
    max_risk_atr: float = 3.0                # skip the trade if widening the stop needs more than this x ATR
    stop_check_bars: int = 6                 # Kronos stop check looks at the paths' first 6 candles (30 min on 5m)
    stop_check_pct: float = 10.0             # ... at the 10th percentile of their lowest lows (buy)
    tp2_cap_r: float = 3.0
    min_confidence: float = 0.55
    min_expectancy_r: float = 0.0
    slippage_pips: float = 0.2
    commission_per_lot: float = 0.0         # round trip, account currency
    tm_replays: int = 2000
    tm_lookback_days: int = 60
    tm_seed: int = 19

    # Phase 7 extras (off until each one is backtested)
    require_htf_agreement: bool = False      # 1H forecast direction must agree with the 5m signal
    volatility_filter: bool = False          # widen SL / skip when forecast volatility spikes
    vol_widen_ratio: float = 1.3
    vol_skip_ratio: float = 2.0
    regime_filter: bool = False              # skip breakouts on ranging days
    tm_synthetic: bool = False               # add Kronos paths to the Time Machine pool

    # Claude explanations (Phase 7), never in the hot path
    explain: bool = False
    explain_model: str = "claude-opus-5-5"

    # API. A password (FOREX_PASSWORD) turns on the login; without one the app only listens on this computer.
    password: str | None = None
    auth_mode: str = "none"                  # none | token | supabase
    api_token: str | None = None
    supabase_jwt_secret: str | None = None
    cors_origins: list = field(default_factory=lambda: ["*"])
    host: str = "127.0.0.1"
    port: int = 8000

    # alerts
    alert_webhook_url: str | None = None
    ntfy_topic: str | None = None
    calendar_url: str | None = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    calendar_file: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(APP_DIR / ".env")
        c = cls()
        c.db_url = _env("FOREX_DB_URL", c.db_url)
        c.redis_url = _env("FOREX_REDIS_URL", _env("REDIS_URL"))
        c.provider = _env("FOREX_PROVIDER", c.provider).lower()
        c.pairs = _env_list("FOREX_PAIRS", c.pairs)
        c.price_basis = _env("FOREX_PRICE_BASIS", c.price_basis)
        c.oanda_token = _env("OANDA_TOKEN")
        c.oanda_account = _env("OANDA_ACCOUNT")
        c.oanda_env = _env("OANDA_ENV", c.oanda_env)
        c.eodhd_key = _env("EODHD_API_KEY")
        c.tradermade_key = _env("TRADERMADE_API_KEY")
        c.backfill_days = _env_int("FOREX_BACKFILL_DAYS", c.backfill_days)
        c.poll_seconds = _env_float("FOREX_POLL_SECONDS", c.poll_seconds)
        c.sim_speed = _env_float("FOREX_SIM_SPEED", c.sim_speed)
        c.sim_start = _env("FOREX_SIM_START", c.sim_start)
        c.sim_history_days = _env_int("FOREX_SIM_HISTORY_DAYS", c.sim_history_days)
        c.sim_seed = _env_int("FOREX_SIM_SEED", c.sim_seed)
        c.model_backend = _env("FOREX_MODEL_BACKEND", c.model_backend).lower()
        c.kronos_repo = _env("KRONOS_REPO", c.kronos_repo)
        c.kronos_model = _env("KRONOS_MODEL", c.kronos_model)
        c.kronos_tokenizer = _env("KRONOS_TOKENIZER", c.kronos_tokenizer)
        c.kronos_checkpoint = _env("KRONOS_CHECKPOINT")
        c.kronos_tokenizer_checkpoint = _env("KRONOS_TOKENIZER_CHECKPOINT")
        c.kronos_device = _env("KRONOS_DEVICE")
        c.max_context = _env_int("KRONOS_MAX_CONTEXT", c.max_context)
        c.lookback = _env_int("FOREX_LOOKBACK", c.lookback)
        c.horizon = _env_int("FOREX_HORIZON", c.horizon)
        c.paths = _env_int("FOREX_PATHS", c.paths)
        c.temperature = _env_float("FOREX_TEMPERATURE", c.temperature)
        c.top_p = _env_float("FOREX_TOP_P", c.top_p)
        c.chart_forecasts = _env_bool("FOREX_CHART_FORECASTS", c.chart_forecasts)
        c.model_timeout_s = _env_float("FOREX_MODEL_TIMEOUT_S", c.model_timeout_s)
        c.sessions = [s.lower() for s in _env_list("FOREX_SESSIONS", c.sessions)]
        c.setup_expiry_minutes = _env_int("FOREX_SETUP_EXPIRY_MIN", c.setup_expiry_minutes)
        c.stop_rule = _env("FOREX_STOP_RULE", c.stop_rule)
        c.min_stop_atr = _env_float("FOREX_MIN_STOP_ATR", c.min_stop_atr)
        c.stop_buffer_spreads = _env_float("FOREX_STOP_BUFFER_SPREADS", c.stop_buffer_spreads)
        c.max_risk_atr = _env_float("FOREX_MAX_RISK_ATR", c.max_risk_atr)
        c.stop_check_bars = _env_int("FOREX_STOP_CHECK_BARS", c.stop_check_bars)
        c.stop_check_pct = _env_float("FOREX_STOP_CHECK_PCT", c.stop_check_pct)
        c.tp2_cap_r = _env_float("FOREX_TP2_CAP_R", c.tp2_cap_r)
        c.min_confidence = _env_float("FOREX_MIN_CONFIDENCE", c.min_confidence)
        c.min_expectancy_r = _env_float("FOREX_MIN_EXPECTANCY_R", c.min_expectancy_r)
        c.slippage_pips = _env_float("FOREX_SLIPPAGE_PIPS", c.slippage_pips)
        c.commission_per_lot = _env_float("FOREX_COMMISSION_PER_LOT", c.commission_per_lot)
        c.tm_replays = _env_int("FOREX_TM_REPLAYS", c.tm_replays)
        c.tm_lookback_days = _env_int("FOREX_TM_LOOKBACK_DAYS", c.tm_lookback_days)
        c.tm_seed = _env_int("FOREX_TM_SEED", c.tm_seed)
        c.require_htf_agreement = _env_bool("FOREX_REQUIRE_HTF", c.require_htf_agreement)
        c.volatility_filter = _env_bool("FOREX_VOLATILITY_FILTER", c.volatility_filter)
        c.regime_filter = _env_bool("FOREX_REGIME_FILTER", c.regime_filter)
        c.tm_synthetic = _env_bool("FOREX_TM_SYNTHETIC", c.tm_synthetic)
        c.explain = _env_bool("FOREX_EXPLAIN", c.explain)
        c.explain_model = _env("FOREX_EXPLAIN_MODEL", c.explain_model)
        c.auth_mode = _env("FOREX_AUTH_MODE", c.auth_mode).lower()
        c.api_token = _env("FOREX_API_TOKEN")
        c.password = _env("FOREX_PASSWORD")
        if c.password and c.auth_mode == "none":
            c.auth_mode, c.api_token = "token", c.password
        c.supabase_jwt_secret = _env("SUPABASE_JWT_SECRET")
        origins = _env("FOREX_CORS_ORIGINS")
        if origins:
            c.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        # with a login the app may listen on the network (phone on the same Wi-Fi, Tailscale); without one, only here
        c.host = _env("FOREX_HOST", "0.0.0.0" if c.auth_mode != "none" else "127.0.0.1")
        c.port = _env_int("FOREX_PORT", c.port)
        c.alert_webhook_url = _env("FOREX_ALERT_WEBHOOK")
        c.ntfy_topic = _env("FOREX_NTFY_TOPIC")
        c.calendar_url = _env("FOREX_CALENDAR_URL", c.calendar_url)
        c.calendar_file = _env("FOREX_CALENDAR_FILE", c.calendar_file)
        return c

    def public(self) -> dict:
        """The non-secret part, for /health and logs."""
        d = dataclasses.asdict(self)
        for k in ("oanda_token", "oanda_account", "eodhd_key", "tradermade_key", "api_token", "password",
                  "supabase_jwt_secret", "alert_webhook_url", "db_url", "redis_url", "ntfy_topic"):
            d[k] = bool(d[k])
        return d


@dataclass
class UserSettings:
    """What the trader sets in the app. Every limit is in the account currency."""
    account_balance: float = 10000.0
    account_currency: str = "USD"
    max_loss_per_trade: float = 50.0
    max_daily_loss: float = 150.0
    max_trades_per_day: int = 3
    min_reward_risk: float = 1.0
    max_lots: float = 5.0
    max_leverage: float = 30.0
    pairs: list = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])
    sessions: list = field(default_factory=lambda: ["london", "newyork", "tokyo"])
    news_blackout_before_min: int = 30
    news_blackout_after_min: int = 15
    friday_cutoff_min: int = 60
    spread_block_multiple: float = 2.0
    min_confidence: float | None = None      # None: use the server default
    alerts_enabled: bool = True
    paused_setups: list = field(default_factory=list)   # ["EURUSD:london", ...] paused by the monitor

    @classmethod
    def from_dict(cls, d: dict | None) -> "UserSettings":
        s = cls()
        if not d:
            return s
        names = {f.name for f in dataclasses.fields(cls)}
        for k, v in d.items():
            if k in names:
                setattr(s, k, v)
        s.validate()
        return s

    def validate(self):
        if self.account_balance <= 0:
            raise ValueError("account_balance must be above 0")
        if self.max_loss_per_trade <= 0:
            raise ValueError("max_loss_per_trade must be above 0")
        if self.max_daily_loss < self.max_loss_per_trade:
            raise ValueError("max_daily_loss must be at least max_loss_per_trade")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be at least 1")
        if self.max_lots <= 0:
            raise ValueError("max_lots must be above 0")
        self.pairs = [p.upper().replace("/", "") for p in self.pairs]
        self.sessions = [s.lower() for s in self.sessions]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
