from conftest import ts

from forexapp.data.candles import Aggregator, Candle, CandleBuilder, Tick, resample
from forexapp.data.validate import compare, find_gaps
from forexapp.timeutil import is_weekend_gap, market_open, next_candle_times, trading_day


def test_builder_closes_minute_on_next_tick_and_uses_bid():
    b = CandleBuilder("bid")
    t0 = ts("2026-11-09 08:00")
    assert b.on_tick(Tick("EURUSD", t0 + 1, 1.1000, 1.1001)) == []
    assert b.on_tick(Tick("EURUSD", t0 + 30, 1.1005, 1.1006)) == []
    assert b.on_tick(Tick("EURUSD", t0 + 59.9, 1.0998, 1.1000)) == []
    out = b.on_tick(Tick("EURUSD", t0 + 60, 1.1001, 1.1002))
    assert len(out) == 1
    c = out[0]
    assert (c.open_time, c.open, c.high, c.low, c.close, c.tick_volume) == (t0, 1.1000, 1.1005, 1.0998, 1.0998, 3)
    assert abs(c.spread - (0.0001 + 0.0001 + 0.0002) / 3) < 1e-12


def test_builder_mid_basis_and_late_ticks_ignored():
    b = CandleBuilder("mid")
    t0 = ts("2026-11-09 08:00")
    b.on_tick(Tick("EURUSD", t0 + 5, 1.1000, 1.1002))
    b.on_tick(Tick("EURUSD", t0 + 65, 1.1010, 1.1012))
    assert b.on_tick(Tick("EURUSD", t0 + 30, 1.0, 1.0)) == []     # belongs to a closed minute
    assert b.late_ticks == 1
    assert b.forming_candle("EURUSD").open == 1.1011


def test_flush_closes_a_quiet_minute_on_time():
    b = CandleBuilder("bid", grace_seconds=2)
    t0 = ts("2026-11-09 08:00")
    b.on_tick(Tick("EURUSD", t0 + 10, 1.1, 1.1001))
    assert b.flush(t0 + 61) == []
    assert [c.open_time for c in b.flush(t0 + 62)] == [t0]


def _minutes(start, n, price=1.1):
    return [Candle("EURUSD", "1m", start + 60 * i, price + i * 1e-4, price + i * 1e-4 + 2e-4, price + i * 1e-4 - 1e-4,
                   price + i * 1e-4 + 1e-4, 5, 1e-4) for i in range(n)]


def test_aggregator_emits_higher_candles_on_their_last_minute():
    agg = Aggregator(("5m", "15m"))
    out = []
    for c in _minutes(ts("2026-11-09 08:00"), 15):
        out += agg.on_1m_close(c)
    fives = [c for c in out if c.tf == "5m"]
    assert [c.open_time for c in fives] == [ts("2026-11-09 08:00"), ts("2026-11-09 08:05"), ts("2026-11-09 08:10")]
    assert [c.open_time for c in out if c.tf == "15m"] == [ts("2026-11-09 08:00")]
    f = fives[0]
    assert f.open == 1.1 and f.tick_volume == 25 and abs(f.close - (1.1 + 4e-4 + 1e-4)) < 1e-12


def test_aggregator_closes_bucket_when_last_minute_missing():
    agg = Aggregator(("5m",))
    mins = _minutes(ts("2026-11-09 08:00"), 6)
    del mins[4]                      # 08:04 had no ticks
    out = []
    for c in mins:
        out += agg.on_1m_close(c)
    assert [c.open_time for c in out] == [ts("2026-11-09 08:00")]   # emitted when 08:05 closed


def test_resample_matches_live_aggregation():
    mins = _minutes(ts("2026-11-09 08:00"), 30)
    agg = Aggregator(("5m",))
    live = [c for m in mins for c in agg.on_1m_close(m)]
    assert [c.to_row() for c in live] == [c.to_row() for c in resample(mins, "5m")]


def test_market_hours_and_weekend():
    assert market_open(ts("2026-11-13 21:59"))          # Friday 16:59 New York (EST)
    assert not market_open(ts("2026-11-13 22:00"))      # Friday 17:00 New York
    assert not market_open(ts("2026-11-14 12:00"))      # Saturday
    assert not market_open(ts("2026-11-15 21:59"))      # Sunday 16:59 New York
    assert market_open(ts("2026-11-15 22:00"))          # Sunday 17:00 New York


def test_future_times_skip_the_weekend():
    t = next_candle_times(ts("2026-11-13 21:50"), 300, 3)
    assert t == [ts("2026-11-13 21:55"), ts("2026-11-15 22:00"), ts("2026-11-15 22:05")]


def test_trading_day_rolls_at_5pm_new_york():
    assert str(trading_day(ts("2026-11-15 22:30"))) == "2026-11-16"   # Sunday evening counts as Monday
    assert str(trading_day(ts("2026-11-16 21:59"))) == "2026-11-16"


def test_gaps_report_weekend_separately():
    a = Candle("EURUSD", "5m", ts("2026-11-13 21:55"), 1, 1, 1, 1)
    b = Candle("EURUSD", "5m", ts("2026-11-15 22:00"), 1, 1, 1, 1)
    c = Candle("EURUSD", "5m", ts("2026-11-15 22:15"), 1, 1, 1, 1)
    assert is_weekend_gap(a.open_time, b.open_time, 300)
    r = find_gaps([a, b, c], "5m")
    assert r["weekend_gaps"] == 1 and r["missing_total"] == 2


def test_compare_in_pips():
    mine = [Candle("EURUSD", "1m", 0, 1.10000, 1.10010, 1.09990, 1.10005)]
    theirs = [Candle("EURUSD", "1m", 0, 1.10001, 1.10010, 1.09990, 1.10005)]
    r = compare(mine, theirs, "EURUSD")
    assert r["matched"] == 1 and r["max_pips"] == 0.1 and r["ok"]
