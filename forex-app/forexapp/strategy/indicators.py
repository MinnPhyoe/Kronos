"""Indicators on candles (time-ordered lists of Candle)."""
from __future__ import annotations


def true_ranges(candles: list) -> list:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: list, n: int = 14) -> float | None:
    """Average true range of the last ``n`` candles (simple average, as trader-exit).
    With fewer candles it averages what there is; None with fewer than 2."""
    if len(candles) < 2:
        return None
    trs = true_ranges(candles[-(n + 1):])[1:]
    return sum(trs) / len(trs) if trs else None


def efficiency_ratio(closes: list) -> float | None:
    """Kaufman efficiency: net move / total path. Near 1 trends, near 0 chops."""
    if len(closes) < 3:
        return None
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


def median(xs: list) -> float | None:
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2
