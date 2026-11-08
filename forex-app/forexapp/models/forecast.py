"""A forecast: many candle paths ahead of the last closed candle, kept separately.

For each candle ahead it reports the median close (the middle path), the 10th
percentile of the lows and the 90th of the highs (the range band, drawn on the
chart and used for the stop check), the 10-90% band of the closes (used for
calibration: the real close should land inside about 80% of the time), and the
share of paths ending above the current price.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import numpy as np

from ..timeutil import iso

O, H, L, C = 0, 1, 2, 3


@dataclass
class Forecast:
    pair: str
    tf: str
    base_time: int            # open time of the last input candle
    last_close: float
    times: list               # open times of the candles ahead
    paths: np.ndarray         # [n_paths, horizon, 4] open/high/low/close
    model: str
    created_at: float = field(default_factory=time.time)
    run_ms: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self):
        p = np.asarray(self.paths, dtype=float)
        # a model predicts each field on its own: make every candle consistent
        hi = p.max(axis=2)
        lo = p.min(axis=2)
        p[:, :, H] = hi
        p[:, :, L] = lo
        self.paths = p

    @property
    def n_paths(self) -> int:
        return int(self.paths.shape[0])

    @property
    def horizon(self) -> int:
        return int(self.paths.shape[1])

    def band(self) -> list:
        p = self.paths
        p10 = np.percentile(p[:, :, L], 10, axis=0)
        p90 = np.percentile(p[:, :, H], 90, axis=0)
        p50 = np.median(p[:, :, C], axis=0)
        c10 = np.percentile(p[:, :, C], 10, axis=0)
        c90 = np.percentile(p[:, :, C], 90, axis=0)
        return [{"time": iso(t), "t": int(t), "p10": float(a), "p50": float(b), "p90": float(c),
                 "c10": float(d), "c90": float(e)}
                for t, a, b, c, d, e in zip(self.times, p10, p50, p90, c10, c90)]

    def direction_prob(self) -> float:
        """Share of paths ending above the current price."""
        return float(np.mean(self.paths[:, -1, C] > self.last_close))

    def median_path(self) -> np.ndarray:
        return np.median(self.paths[:, :, C], axis=0)

    def path_extremes(self) -> tuple:
        """(lowest low, highest high) of each path over the horizon."""
        return self.paths[:, :, L].min(axis=1), self.paths[:, :, H].max(axis=1)

    def forecast_atr(self) -> float:
        """Average candle range across paths: a volatility forecast."""
        return float(np.mean(self.paths[:, :, H] - self.paths[:, :, L]))

    def to_dict(self, include_paths: bool = False, digits: int = 6) -> dict:
        d = {"id": self.id, "pair": self.pair, "tf": self.tf, "base_time": int(self.base_time),
             "base_time_iso": iso(self.base_time), "last_close": self.last_close, "model": self.model,
             "created_at": int(self.created_at), "run_ms": round(self.run_ms, 1), "horizon": self.horizon,
             "n_paths": self.n_paths, "direction_prob": round(self.direction_prob(), 4),
             "forecast_atr": self.forecast_atr(), "band": self.band()}
        if include_paths:
            d["times"] = [int(t) for t in self.times]
            d["paths"] = np.round(self.paths, digits).tolist()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Forecast":
        return cls(d["pair"], d["tf"], int(d["base_time"]), float(d["last_close"]), list(d["times"]),
                   np.asarray(d["paths"], dtype=float), d["model"], float(d.get("created_at", time.time())),
                   float(d.get("run_ms", 0.0)), d.get("id") or uuid.uuid4().hex[:12])
