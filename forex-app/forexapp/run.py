"""Start the services.

    python -m forexapp.run all              # every service in one process (development)
    python -m forexapp.run all --demo       # simulated market, fast clock, demo model: see a London morning in minutes
    python -m forexapp.run feed|model|strategy|api   # one service each (Docker), talking over Redis

Separate services need FOREX_REDIS_URL so they can reach each other.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from .bus import make_bus
from .config import APP_DIR, Config, UserSettings
from .data.store import Store

log = logging.getLogger("forexapp")


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def apply_demo(cfg: Config):
    cfg.provider = "simulated"
    cfg.sim_start = os.environ.get("FOREX_SIM_START") or "demo"
    cfg.sim_speed = float(os.environ.get("FOREX_SIM_SPEED") or 30)
    cfg.db_url = os.environ.get("FOREX_DB_URL") or "sqlite:///" + str(APP_DIR / "data" / "demo.db")
    if cfg.db_url.startswith("sqlite:///") and not os.environ.get("FOREX_DB_URL"):
        p = Path(cfg.db_url[len("sqlite:///"):])
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(p) + suffix)
            if q.exists():
                q.unlink()
    if not os.environ.get("FOREX_MODEL_BACKEND") and not _torch_available():
        cfg.model_backend = "bootstrap"
    if not os.environ.get("FOREX_CALENDAR_URL"):
        cfg.calendar_url = None           # the simulated market has no real news
        cfg.calendar_file = cfg.calendar_file or str(APP_DIR / "data" / "calendar.demo.json")
        if not Path(cfg.calendar_file).exists():
            cfg.calendar_file = None
    cfg.tm_lookback_days = min(cfg.tm_lookback_days, cfg.sim_history_days)


async def run_feed(cfg, store, bus):
    from .data.feed import FeedService
    from .data.providers import make_provider
    provider = make_provider(cfg)
    backfill = cfg.sim_history_days if cfg.provider == "simulated" else cfg.backfill_days
    feed = FeedService(provider, store, bus, cfg.pairs, cfg.price_basis, backfill)
    log.info("feed: %s, pairs %s", provider.name, ", ".join(cfg.pairs))
    await feed.run()


async def run_model(cfg, store, bus):
    from .models.kronos_service import make_forecaster
    from .models.service import ModelService
    fc = make_forecaster(cfg)
    log.info("model: %s", getattr(fc, "name", cfg.model_backend))
    await ModelService(cfg, store, bus, fc).run()


async def run_strategy(cfg, store, bus):
    from .signals.explain import make_explainer
    from .strategy.alerts import Alerter
    from .strategy.news import Calendar
    from .strategy.service import StrategyService
    cal = Calendar(cfg.calendar_url, cfg.calendar_file)
    alerter = Alerter(bus, cfg.alert_webhook_url, cfg.ntfy_topic)
    svc = StrategyService(cfg, store, bus, alerter, cal, make_explainer(cfg))

    async def beat():
        while True:
            await bus.publish("status", {"type": "status", "service": "strategy", "ok": True,
                                         "setups": len(svc.setups), "tracked": len(svc.tracked), "ts": time.time()})
            await asyncio.sleep(5)

    async def monitor():
        from .monitor import auto_pause
        while True:
            await asyncio.sleep(3600)
            try:
                for p in await asyncio.to_thread(auto_pause, store):
                    await alerter.send("pause:%s:%d" % (p["setup"], int(time.time()) // 86400), "paused",
                                       "Paused %s" % p["setup"],
                                       "Last %d trades averaged %+.2f R: the setup is paused until you resume it in Settings."
                                       % (p["trades"], p["expectancy_r"]), p)
            except Exception:
                log.exception("auto-pause")

    await asyncio.gather(svc.run(), beat(), monitor())


def _lan_ip() -> str | None:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


async def run_api(cfg, store, bus, open_browser=False):
    import uvicorn
    from .api.server import create_app
    app = create_app(cfg, store, bus)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning"))
    local = "http://localhost:%d" % cfg.port
    lines = ["", "  FX app is running:  %s" % local]
    if cfg.host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            lines.append("  From your phone (same Wi-Fi or Tailscale):  http://%s:%d" % (ip, cfg.port))
        lines.append("  Login: the password you set (FOREX_PASSWORD).")
    else:
        lines.append("  Private: only this computer can open it. Set a password in .env to use it from your phone.")
    lines += ["  Prices: %s.  Stop with Ctrl+C." % cfg.provider, ""]
    print("\n".join(lines), flush=True)
    if open_browser:
        import webbrowser
        asyncio.get_running_loop().call_later(2.0, lambda: webbrowser.open(local))
    await server.serve()


async def main_async(which: str, cfg: Config, open_browser: bool = False):
    store = Store(cfg.db_url)
    if store.get_settings() is None:
        store.put_settings(UserSettings(pairs=list(cfg.pairs), sessions=list(cfg.sessions)).to_dict())
    bus = make_bus(cfg.redis_url)
    if which != "all" and cfg.redis_url is None:
        log.warning("running '%s' alone without FOREX_REDIS_URL: it cannot talk to the other services", which)
    runners = {"feed": run_feed, "model": run_model, "strategy": run_strategy,
               "api": lambda c, s, b: run_api(c, s, b, open_browser)}
    names = list(runners) if which == "all" else [which]
    # the API and the strategy subscribe before the feed starts publishing
    order = sorted(names, key=lambda n: {"api": 0, "strategy": 1, "model": 2, "feed": 3}[n])
    tasks = []
    for n in order:
        tasks.append(asyncio.create_task(runners[n](cfg, store, bus), name=n))
        await asyncio.sleep(0.3)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        if t.exception():
            log.error("service %s stopped: %r", t.get_name(), t.exception())
    for t in pending:
        t.cancel()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forexapp.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("service", choices=["all", "feed", "model", "strategy", "api"])
    ap.add_argument("--demo", action="store_true", help="simulated market on a fast clock (no keys, no GPU)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="open the app in the browser once it is up")
    ap.add_argument("--log", default="INFO")
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if a.demo:
        apply_demo(cfg)
        log.info("demo: simulated market at %gx speed, model backend %s, database %s",
                 cfg.sim_speed, cfg.model_backend, cfg.db_url)
        if cfg.model_backend == "bootstrap":
            log.info("demo: torch is not installed, so forecasts come from the bootstrap stand-in, not Kronos")
    if a.port:
        cfg.port = a.port
    try:
        asyncio.run(main_async(a.service, cfg, a.open))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
