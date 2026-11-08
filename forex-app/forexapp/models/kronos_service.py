"""The Kronos wrapper (Phase 2): a forecast range for any pair and timeframe.

1. Load once at startup: KronosTokenizer and Kronos from Hugging Face (or a
   fine-tuned checkpoint from Phase 6), then KronosPredictor.
2. Input: the last 400-512 closed candles (open, high, low, close, tick
   volume) and their timestamps. ``amount`` is 0: spot forex has no traded
   value, and the fine-tuning data uses the same rule.
3. Future timestamps: the next N candle times, skipping the weekend close.
4. Horizon: about 12-24 candles ahead.
5. Many paths: each path is one sample (sample_count=1, T=1.0, top_p=0.9)
   and every path is kept, never averaged. All paths of one pair go through
   ``predict_batch`` together, which is one forward pass per step instead of
   30 separate ``predict`` calls.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np

from ..config import TIMEFRAMES, Config
from ..timeutil import next_candle_times
from .forecast import Forecast

log = logging.getLogger(__name__)


class KronosUnavailable(RuntimeError):
    pass


def _auto_device():
    import torch
    if torch.cuda.is_available():
        return "cuda:0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class KronosForecaster:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.predictor = None
        self.lock = threading.Lock()
        self.load_error: str | None = None
        ckpt = cfg.kronos_checkpoint
        self.name = "Kronos (%s)" % (Path(ckpt).name if ckpt else cfg.kronos_model.split("/")[-1])

    def ready(self) -> bool:
        return self.predictor is not None

    def load(self):
        """Load the tokenizer and model once. Raises KronosUnavailable with the reason."""
        if self.predictor is not None:
            return
        repo = Path(self.cfg.kronos_repo)
        if (repo / "model" / "kronos.py").exists() and str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        try:
            import pandas  # noqa: F401
            import torch  # noqa: F401
            from model import Kronos, KronosPredictor, KronosTokenizer
        except Exception as e:
            self.load_error = "Kronos needs torch and the Kronos repo on the path (%s)" % e
            raise KronosUnavailable(self.load_error) from e
        tok_src = self.cfg.kronos_tokenizer_checkpoint or self.cfg.kronos_tokenizer
        model_src = self.cfg.kronos_checkpoint or self.cfg.kronos_model
        device = self.cfg.kronos_device or _auto_device()
        t0 = time.perf_counter()
        try:
            tokenizer = KronosTokenizer.from_pretrained(tok_src)
            model = Kronos.from_pretrained(model_src)
            tokenizer.eval()
            model.eval()
            self.predictor = KronosPredictor(model, tokenizer, device=device, max_context=self.cfg.max_context)
        except Exception as e:
            self.load_error = "could not load %s / %s: %s" % (model_src, tok_src, e)
            raise KronosUnavailable(self.load_error) from e
        log.info("loaded %s on %s in %.1fs", model_src, device, time.perf_counter() - t0)

    # ------------------------------------------------------------------ forecasting
    @staticmethod
    def _frame(candles: list):
        import pandas as pd
        df = pd.DataFrame({
            "open": [c.open for c in candles], "high": [c.high for c in candles],
            "low": [c.low for c in candles], "close": [c.close for c in candles],
            "volume": [float(c.tick_volume) for c in candles], "amount": [0.0] * len(candles)})
        ts = pd.Series(pd.to_datetime([c.open_time for c in candles], unit="s"))
        return df, ts

    def forecast_many(self, jobs: list, horizon: int, n_paths: int) -> list:
        """jobs: [(candles, pair, tf)]. One batched predict call for all paths of all jobs
        when they share an input length, else one call per job."""
        import pandas as pd
        self.load()
        prepared = []
        for candles, pair, tf in jobs:
            candles = candles[-self.cfg.lookback:]
            if len(candles) < 64:
                raise ValueError("need at least 64 closed candles to forecast %s %s, have %d" % (pair, tf, len(candles)))
            df, ts = self._frame(candles)
            fut = next_candle_times(candles[-1].open_time, TIMEFRAMES[tf], horizon)
            yts = pd.Series(pd.to_datetime(fut, unit="s"))
            prepared.append((candles, pair, tf, df, ts, fut, yts))
        groups: dict[int, list] = {}
        for i, p in enumerate(prepared):
            groups.setdefault(len(p[0]), []).append(i)
        results: list = [None] * len(prepared)
        for _, idxs in groups.items():
            t0 = time.perf_counter()
            dfs, xts, yts = [], [], []
            for i in idxs:
                _, _, _, df, ts, _, yt = prepared[i]
                dfs += [df] * n_paths
                xts += [ts] * n_paths
                yts += [yt] * n_paths
            with self.lock:
                preds = self._predict(dfs, xts, yts, horizon)
            ms = (time.perf_counter() - t0) * 1000 / len(idxs)
            for k, i in enumerate(idxs):
                candles, pair, tf, _, _, fut, _ = prepared[i]
                chunk = preds[k * n_paths:(k + 1) * n_paths]
                paths = np.stack([p[["open", "high", "low", "close"]].to_numpy(dtype=float) for p in chunk])
                f = Forecast(pair, tf, candles[-1].open_time, candles[-1].close, fut, paths, self.name)
                f.run_ms = ms
                results[i] = f
        return results

    def forecast(self, candles: list, pair: str, tf: str, horizon: int, n_paths: int) -> Forecast:
        return self.forecast_many([(candles, pair, tf)], horizon, n_paths)[0]

    def _predict(self, dfs, xts, yts, horizon):
        kw = dict(pred_len=horizon, T=self.cfg.temperature, top_k=0, top_p=self.cfg.top_p, sample_count=1,
                  verbose=False)
        if hasattr(self.predictor, "predict_batch"):
            try:
                return self.predictor.predict_batch(dfs, xts, yts, **kw)
            except (RuntimeError, MemoryError) as e:   # e.g. GPU out of memory: fall back to one path at a time
                log.warning("predict_batch failed (%s); predicting one path at a time", e)
        return [self.predictor.predict(df, x, y, **kw) for df, x, y in zip(dfs, xts, yts)]


def make_forecaster(cfg: Config):
    """Kronos, or the bootstrap stand-in when FOREX_MODEL_BACKEND=bootstrap."""
    if cfg.model_backend == "bootstrap":
        from .bootstrap import BootstrapForecaster
        return BootstrapForecaster()
    return KronosForecaster(cfg)
