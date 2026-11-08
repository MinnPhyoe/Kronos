"""Regime detection (Phase 7): label the day trending or ranging.

From the last 24 one-hour candles: the efficiency ratio (net move over the
total path; trends are efficient, ranges are not) and the ATR compared with
its own 20-day median. A 1H forecast's slope, if given, tips the balance.
Breakouts are used in trends and skipped in ranges (``FOREX_REGIME_FILTER``).
"""
from __future__ import annotations

from .indicators import atr, efficiency_ratio, median


def detect(candles_1h: list, forecast_1h=None) -> dict:
    if len(candles_1h) < 30:
        return {"label": "unknown", "reason": "fewer than 30 one-hour candles"}
    closes = [c.close for c in candles_1h[-25:]]
    er = efficiency_ratio(closes)
    recent_atr = atr(candles_1h[-15:], 14)
    hist = [atr(candles_1h[max(0, i - 15):i], 14) for i in range(15, len(candles_1h), 6)]
    base = median(hist) or recent_atr
    atr_ratio = recent_atr / base if base else 1.0
    slope = None
    if forecast_1h is not None:
        med = forecast_1h.median_path()
        slope = (med[-1] - forecast_1h.last_close) / (recent_atr or 1.0)
    score = er + (0.1 if atr_ratio > 1.2 else 0.0) + (0.1 if slope is not None and abs(slope) > 1.0 else 0.0)
    label = "trending" if score >= 0.3 else "ranging" if score < 0.2 else "mixed"
    return {"label": label, "efficiency": round(er, 3), "atr_ratio": round(atr_ratio, 2),
            "forecast_slope_atr": round(slope, 2) if slope is not None else None}
