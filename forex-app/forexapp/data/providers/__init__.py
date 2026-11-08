"""Pick one provider and stick with it: training, backtests and live signals
must all use the same candles."""
from __future__ import annotations

from ...config import Config
from ...timeutil import parse_iso
from .base import Provider, ProviderError


def make_provider(cfg: Config) -> Provider:
    p = cfg.provider
    if p == "simulated":
        from .simulated import SimulatedProvider, demo_start
        start = parse_iso(cfg.sim_start) if cfg.sim_start and cfg.sim_start != "demo" else None
        if cfg.sim_start == "demo":
            start = demo_start()
        return SimulatedProvider(cfg.pairs, speed=cfg.sim_speed, start=start,
                                 history_days=cfg.sim_history_days, seed=cfg.sim_seed)
    if p == "oanda":
        from .oanda import OandaProvider
        return OandaProvider(cfg.oanda_token, cfg.oanda_account, cfg.oanda_env, cfg.price_basis)
    if p == "eodhd":
        from .eodhd import EodhdProvider
        return EodhdProvider(cfg.eodhd_key)
    if p == "tradermade":
        from .tradermade import TraderMadeProvider
        return TraderMadeProvider(cfg.tradermade_key)
    raise ProviderError("unknown FOREX_PROVIDER %r (simulated, oanda, eodhd, tradermade)" % p)


__all__ = ["Provider", "ProviderError", "make_provider"]
