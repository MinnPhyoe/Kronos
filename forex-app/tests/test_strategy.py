import pytest
from conftest import candle, make_forecast, ts

from forexapp import instruments
from forexapp.config import UserSettings
from forexapp.strategy import timemachine as tm
from forexapp.strategy.exits import PlanRejected, forecast_levels, plan_exit
from forexapp.strategy.indicators import atr
from forexapp.strategy.news import EconEvent, blackout, parse_events
from forexapp.strategy.risk import check_order
from forexapp.strategy.scanner import evaluate_entry, opening_range
from forexapp.strategy.sessions import SESSIONS, in_quiet_window, window
from forexapp.strategy.sizing import size_position

EU = instruments.get("EURUSD")


# ---------------------------------------------------------------- sessions
def test_london_range_is_0800_london_all_year():
    from datetime import date
    w = window(SESSIONS["london"], date(2026, 11, 9))
    assert w.range_start == ts("2026-11-09 08:00") and w.range_end == ts("2026-11-09 08:15")
    s = window(SESSIONS["london"], date(2026, 7, 6))
    assert s.range_start == ts("2026-07-06 07:00")               # British Summer Time


def test_new_york_when_us_and_uk_clocks_differ():
    from datetime import date
    # 2026: US clocks change on 8 March, UK on 29 March. New York 08:00 is 12:00 UTC in between.
    assert window(SESSIONS["newyork"], date(2026, 3, 16)).range_start == ts("2026-03-16 12:00")
    assert window(SESSIONS["newyork"], date(2026, 3, 2)).range_start == ts("2026-03-02 13:00")
    assert window(SESSIONS["newyork"], date(2026, 3, 16), 90).expiry == ts("2026-03-16 13:45")


def test_tokyo_only_for_jpy_and_no_weekend_sessions():
    from datetime import date
    assert SESSIONS["tokyo"].applies("USDJPY") and not SESSIONS["tokyo"].applies("EURUSD")
    assert window(SESSIONS["london"], date(2026, 11, 14)) is None          # Saturday


def test_quiet_window_before_us_data():
    assert in_quiet_window("newyork", ts("2026-11-09 13:25"))              # 08:25 New York
    assert not in_quiet_window("newyork", ts("2026-11-09 13:40"))
    assert not in_quiet_window("london", ts("2026-11-09 13:25"))


# ---------------------------------------------------------------- scanner and entry
def _london(day="2026-11-09"):
    from datetime import date
    return window(SESSIONS["london"], date.fromisoformat(day), 90)


def _range_candles(t0):
    return [candle("EURUSD", "5m", t0, 1.1000, 1.1010, 1.0995, 1.1005),
            candle("EURUSD", "5m", t0 + 300, 1.1005, 1.1012, 1.1000, 1.1008),
            candle("EURUSD", "5m", t0 + 600, 1.1008, 1.1009, 1.0990, 1.1001)]


def test_opening_range_needs_all_three_candles():
    w = _london()
    cs = _range_candles(w.range_start)
    r = opening_range(cs, "EURUSD", w)
    assert (r.high, r.low, r.bars) == (1.1012, 1.0990, 3)
    assert opening_range(cs[:2], "EURUSD", w) is None


def test_trigger_is_first_close_beyond_not_a_wick():
    w = _london()
    t = w.range_end
    cs = _range_candles(w.range_start) + [
        candle("EURUSD", "5m", t, 1.1001, 1.1020, 1.1000, 1.1010),        # wick above only
        candle("EURUSD", "5m", t + 300, 1.1010, 1.1015, 1.1005, 1.1012),  # close exactly at the high
        candle("EURUSD", "5m", t + 600, 1.1012, 1.1020, 1.1010, 1.1016),  # close above: trigger
        candle("EURUSD", "5m", t + 900, 1.1016, 1.1017, 1.0980, 1.0985),  # later close below doesn't reset
    ]
    r = opening_range(cs, "EURUSD", w)
    assert evaluate_entry(r, cs, t + 600).status == "waiting"
    e = evaluate_entry(r, cs, t + 900)                         # the trigger candle has closed
    assert (e.status, e.side, e.price, e.trigger_time) == ("triggered", "buy", 1.1016, t + 900)
    assert evaluate_entry(r, cs, t + 2000).side == "buy"
    assert evaluate_entry(r, cs, t + 899).status == "waiting"    # still forming: does not count


def test_sell_trigger_and_expiry():
    w = _london()
    t = w.range_end
    cs = _range_candles(w.range_start) + [candle("EURUSD", "5m", t, 1.1001, 1.1002, 1.0980, 1.0985)]
    r = opening_range(cs, "EURUSD", w)
    assert evaluate_entry(r, cs, t + 300).side == "sell"
    quiet = _range_candles(w.range_start) + [candle("EURUSD", "5m", t + 300 * i, 1.1, 1.1005, 1.0995, 1.1)
                                              for i in range(20)]
    assert evaluate_entry(r, quiet, w.expiry).status == "expired"


# ---------------------------------------------------------------- exits
def test_midpoint_stop_with_spread_buffer_and_1r_target():
    p = plan_exit(EU, "buy", 1.10200, 1.10150, 1.10050, atr=0.0004, spread=0.00008)
    assert p.stop == 1.10092           # midpoint 1.10100 minus one spread, rounded down
    assert p.tp1 == 1.10308 and abs(p.risk - 0.00108) < 1e-9
    assert p.tp2 == 1.10416            # rules-only: 2R


def test_min_atr_distance_moves_the_stop():
    p = plan_exit(EU, "buy", 1.10200, 1.10190, 1.10170, atr=0.0010, spread=0.00008)
    assert p.stop == pytest.approx(1.10150) and "minimum distance" in p.notes[0]


def test_kronos_check_widens_or_skips():
    p = plan_exit(EU, "buy", 1.10200, 1.10150, 1.10050, atr=0.0010, spread=0.00008, path_extreme=1.10050,
                  median_reach=1.10500)
    assert p.widened and p.stop == pytest.approx(1.10040)        # one pip beyond the paths' 10th percentile
    with pytest.raises(PlanRejected) as e:
        plan_exit(EU, "buy", 1.10200, 1.10150, 1.10050, atr=0.0002, spread=0.00008, path_extreme=1.09900,
                  median_reach=1.11)
    assert e.value.kind.startswith("forecast paths dip")


def test_median_short_of_1r_means_no_signal_and_tp2_capped_at_3r():
    with pytest.raises(PlanRejected):
        plan_exit(EU, "buy", 1.10200, 1.10150, 1.10050, atr=0.0004, spread=0.00008, median_reach=1.10250)
    p = plan_exit(EU, "buy", 1.10200, 1.10150, 1.10050, atr=0.0004, spread=0.00008, median_reach=1.20)
    assert p.tp2 == pytest.approx(1.10200 + 3 * p.risk, abs=1e-5)


def test_sell_plan_mirrors():
    p = plan_exit(EU, "sell", 1.10000, 1.10150, 1.10050, atr=0.0004, spread=0.00008)
    assert p.stop == 1.10108 and p.tp1 == 1.09892


def test_forecast_levels_use_paths():
    up = [[(1.1, 1.1 + 0.0001 * (j + 1), 1.1 - 0.0001, 1.1 + 0.0001 * j) for j in range(10)] for _ in range(10)]
    f = make_forecast("EURUSD", ts("2026-11-09 08:15"), 1.1, up)
    ext, reach = forecast_levels(f, "buy", 0.0001, check_bars=6)
    assert ext == pytest.approx(1.0999) and reach == pytest.approx(1.101)


# ---------------------------------------------------------------- sizing
def test_pip_values_per_lot():
    assert instruments.pip_value_per_lot("EURUSD", {}) == pytest.approx(10.0)
    assert instruments.pip_value_per_lot("USDJPY", {"USDJPY": 150.0}) == pytest.approx(1000 / 150)
    assert instruments.pip_value_per_lot("XAUUSD", {}) == pytest.approx(10.0)
    assert instruments.pip_value_per_lot("EURGBP", {"GBPUSD": 1.25}) == pytest.approx(12.5)


def test_lots_from_max_loss_round_down():
    st = UserSettings(account_balance=10000, max_loss_per_trade=50, max_lots=5)
    s = size_position("EURUSD", 1.10200, 1.10092, st, {})       # 10.8 pips
    assert s.lots == 0.46 and s.bound_by == "max loss" and s.max_loss <= 50
    far = size_position("EURUSD", 1.1, 1.0, st, {})              # 1000 pips: under 0.01 lot
    assert far.lots == 0.0


def test_leverage_cap():
    st = UserSettings(account_balance=1000, max_loss_per_trade=500, max_lots=50, max_leverage=10)
    s = size_position("EURUSD", 1.1, 1.0999, st, {"EURUSD": 1.1})
    assert s.bound_by == "leverage" and s.lots == 0.09


# ---------------------------------------------------------------- risk
def _order(**kw):
    o = {"id": "x", "pair": "EURUSD", "side": "buy", "entry": 1.102, "sl": 1.101, "tp1": 1.103, "lots": 0.5,
         "max_loss": 50.0, "setup_key": "EURUSD:london"}
    o.update(kw)
    return o


NOW = ts("2026-11-09 08:30")


def test_risk_pass():
    r = check_order(_order(), UserSettings(), NOW, entry_window=(NOW - 60, NOW + 60), spread_now=0.0001,
                    spread_normal=0.0001)
    assert r.verdict == "PASS", r.reasons


@pytest.mark.parametrize("kw,ctx,word", [
    ({"max_loss": 60.0}, {}, "loss"),
    ({}, {"today_entries": [{"max_loss": 50}, {"max_loss": 50}, {"max_loss": 40}]}, "daily loss"),
    ({}, {"today_entries": [{"max_loss": 1}] * 3}, "trades"),
    ({"tp1": 1.1025}, {}, "reward:risk"),
    ({"sl": 1.103}, {}, "stop"),
    ({}, {"spread_now": 0.0005}, "spread"),
    ({}, {"active": [{"id": "other", "pair": "EURUSD", "side": "buy"}]}, "duplicate"),
    ({}, {"events": [EconEvent(NOW + 600, "USD", "CPI", "High")]}, "news"),
    ({}, {"entry_window": (NOW + 60, NOW + 600)}, "window"),
    ({}, {"quiet": True}, "window"),
])
def test_risk_blocks(kw, ctx, word):
    args = {"entry_window": (NOW - 60, NOW + 60), "spread_now": 0.0001, "spread_normal": 0.0001}
    args.update(ctx)
    r = check_order(_order(**kw), UserSettings(), NOW, **args)
    assert r.verdict == "BLOCK" and any(x.startswith(word) for x in r.reasons), r.reasons


def test_friday_cutoff_and_paused():
    fri = ts("2026-11-13 21:30")                                  # 16:30 New York, 30 min before the close
    r = check_order(_order(), UserSettings(), fri, spread_now=1e-4, spread_normal=1e-4)
    assert any(x.startswith("friday") for x in r.reasons)
    r = check_order(_order(), UserSettings(paused_setups=["EURUSD:london"]), NOW, spread_now=1e-4, spread_normal=1e-4)
    assert any(x.startswith("paused") for x in r.reasons)


def test_news_blackout_and_parsing():
    evs = parse_events([{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
                         "impact": "High"}, {"title": "x", "country": "JPY", "date": "bad", "impact": "High"}])
    assert len(evs) == 1 and evs[0].time == ts("2026-11-06 13:30")
    assert blackout(evs, "XAUUSD", ts("2026-11-06 13:05"), 30, 15)          # gold trades on USD news
    assert blackout(evs, "EURGBP", ts("2026-11-06 13:05"), 30, 15) is None
    assert blackout(evs, "EURUSD", ts("2026-11-06 13:46"), 30, 15) is None


# ---------------------------------------------------------------- time machine
def _pool_days(kind, days=30, start="2026-10-01 00:00"):
    from forexapp.data.candles import Candle
    out, p = [], 1.1
    t0 = ts(start)
    for i in range(days * 288):
        c = p * (1.0001 if kind == "up" else 1.0)
        out.append(Candle("EURUSD", "5m", t0 + 300 * i, p, max(p, c) * 1.00005, min(p, c) * 0.99995, c))
        p = c
    return out


def test_time_machine_is_deterministic_and_reads_the_pool():
    tz = SESSIONS["london"].tz
    up = _pool_days("up")
    before = up[-1].open_time + 300
    pool = tm.build_pool(up, tz, before, 60)
    a = tm.simulate(pool, "buy", 1.2, 1.2001, 1.198, 1.2015, before, 24, 0.0001, tz, 500, 19)
    b = tm.simulate(pool, "buy", 1.2, 1.2001, 1.198, 1.2015, before, 24, 0.0001, tz, 500, 19)
    assert a == b
    assert a["p_target"] == 1.0 and "Strongly toward the target" in a["verdict"]
    s = tm.simulate(pool, "sell", 1.2, 1.2, 1.2015, 1.1985, before, 24, 0.0001, tz, 500, 19)
    assert s["p_stop"] == 1.0


def test_time_machine_coin_flip_verdict():
    mc = {"p_target": 0.30, "p_stop": 0.29, "p_neither": 0.41, "r_at_target": 1.0, "avg_R": 0.01}
    assert tm.verdict(mc).startswith("Close to a coin flip")
    assert tm.verdict_notes(mc)


def test_atr():
    cs = [candle("EURUSD", "5m", i * 300, 1.0, 1.001, 0.999, 1.0) for i in range(20)]
    assert atr(cs, 14) == pytest.approx(0.002)
