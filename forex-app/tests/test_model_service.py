"""The model service runs setup forecasts before chart forecasts, in their own batch."""
import asyncio

from forexapp.config import Config
from forexapp.data.candles import Candle
from forexapp.models.service import ModelService


class FakeStore:
    def get_candles(self, pair, tf, n, end=None):
        t = (end - 1) if end else 3000
        return [Candle(pair, tf, t - 300 * k, 1.0, 1.0, 1.0, 1.0) for k in range(3)][::-1]

    def log_health(self, *a, **k):
        pass

    def save_forecast(self, *a, **k):
        pass


class FakeBus:
    def __init__(self):
        self.sent = []

    async def publish(self, ch, msg):
        self.sent.append((ch, msg))


class FakeForecast:
    def to_dict(self, include_paths=False):
        return {}


class FakeForecaster:
    name = "fake"

    def __init__(self):
        self.calls = []

    def forecast_many(self, jobs, horizon, n_paths):
        self.calls.append(sorted(pair for _, pair, _ in jobs))
        return [FakeForecast() for _ in jobs]


def _run_until(svc, fc, calls):
    async def go():
        task = asyncio.create_task(svc._work())
        svc.wake.set()
        for _ in range(200):
            if len(fc.calls) >= calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
    asyncio.run(go())


def test_setup_requests_run_alone_before_chart_forecasts():
    fc = FakeForecaster()
    svc = ModelService(Config(), FakeStore(), FakeBus(), fc)
    svc.ok = True
    svc.pending = {
        ("EURUSD", "5m", "chart"): {"pair": "EURUSD", "tf": "5m", "reason": "chart", "base_time": 3000},
        ("GBPUSD", "5m", "chart"): {"pair": "GBPUSD", "tf": "5m", "reason": "chart", "base_time": 3000},
        ("USDJPY", "5m", "setup"): {"pair": "USDJPY", "tf": "5m", "reason": "setup", "base_time": 3000},
    }
    _run_until(svc, fc, 2)
    assert fc.calls == [["USDJPY"], ["EURUSD", "GBPUSD"]]


def test_chart_forecasts_wait_when_new_setup_requests_arrive():
    fc = FakeForecaster()
    svc = ModelService(Config(), FakeStore(), FakeBus(), fc)
    svc.ok = True
    original = fc.forecast_many

    def slow_first(jobs, horizon, n_paths):
        out = original(jobs, horizon, n_paths)
        if len(fc.calls) == 1:   # a new setup request arrives while the first batch runs
            svc.pending[("XAUUSD", "5m", "setup")] = {"pair": "XAUUSD", "tf": "5m", "reason": "setup", "base_time": 3000}
        return out

    fc.forecast_many = slow_first
    svc.pending = {
        ("EURUSD", "5m", "chart"): {"pair": "EURUSD", "tf": "5m", "reason": "chart", "base_time": 3000},
        ("USDJPY", "5m", "setup"): {"pair": "USDJPY", "tf": "5m", "reason": "setup", "base_time": 3000},
    }
    _run_until(svc, fc, 3)
    assert fc.calls == [["USDJPY"], ["XAUUSD"], ["EURUSD"]]
