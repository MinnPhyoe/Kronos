"""The playbook strategies fire on the patterns they describe, in the right direction, with sane plans."""
from forexapp import instruments
from forexapp.data.candles import Candle
from forexapp.strategy import playbook

EU = instruments.get("EURUSD")


def c(tf, t, o, h, l, cl):
    return Candle("EURUSD", tf, t, o, h, l, cl, 10, 0.00008)


def daily(closes, rng=0.0040):
    out = []
    for i, cl in enumerate(closes):
        o = closes[i - 1] if i else cl
        out.append(c("1d", i * 86400, o, max(o, cl) + rng / 2, min(o, cl) - rng / 2, cl))
    return out


def hourly(price, n=40, start=10_000_000, up=True):
    out, p = [], price
    for i in range(n):
        nxt = p + (0.00002 if up else -0.00002)
        out.append(c("1h", start + i * 3600, p, max(p, nxt) + 0.0002, min(p, nxt) - 0.0002, nxt))
        p = nxt
    return out


def sane(p):
    pl = p["plan"]
    if p["side"] == "buy":
        assert pl["stop"] < pl["entry"] < pl["target"]
    else:
        assert pl["target"] < pl["entry"] < pl["stop"]


def test_crt_buy_after_a_sweep_of_the_low_and_a_structure_shift():
    c1 = c("4h", 0, 1.1050, 1.1100, 1.1000, 1.1020)
    c2 = c("4h", 14400, 1.1020, 1.1030, 1.0980, 1.1010)          # swept 1.1000, closed back inside
    ltf, p = [], 1.0990
    for i in range(30):                                          # dips, then rallies past its last swing high
        nxt = p + (-0.0002 if i < 8 else 0.0003 if i > 14 else (0.0004 if i % 2 else -0.0004))
        ltf.append(c("15m", 14400 + i * 900, p, max(p, nxt) + 0.0001, min(p, nxt) - 0.0001, nxt))
        p = nxt
    found = playbook.crt("EURUSD", EU, [c1, c2], ltf, "H4", "M15")
    assert found and found[0]["side"] == "buy"
    sane(found[0])
    assert found[0]["plan"]["target"] == 1.1100 and found[0]["plan"]["target_50"] == 1.1050


def test_crt_needs_a_close_back_inside():
    c1 = c("4h", 0, 1.1050, 1.1100, 1.1000, 1.1020)
    c2 = c("4h", 14400, 1.1020, 1.1030, 1.0980, 1.0985)          # swept and closed outside: a breakout, not CRT
    ltf = [c("15m", 14400 + i * 900, 1.0985, 1.0990, 1.0980, 1.0985) for i in range(30)]
    assert not [x for x in playbook.crt("EURUSD", EU, [c1, c2], ltf, "H4", "M15") if x["side"] == "buy"]


def test_20_day_breakout_up():
    closes = [1.1000 + 0.0005 * ((i % 4) - 2) for i in range(30)] + [1.1100]
    found = [x for x in playbook.daily_setups("EURUSD", EU, daily(closes), hourly(1.1100)) if x["strategy"] == "20-day breakout"]
    assert found and found[0]["side"] == "buy"
    sane(found[0])


def test_rsi_bounce_after_oversold():
    closes = [1.2000 - 0.0040 * i for i in range(30)] + [1.0900]  # falling hard, then an up day
    found = [x for x in playbook.daily_setups("EURUSD", EU, daily(closes, 0.002), hourly(1.0900)) if x["strategy"] == "RSI bounce"]
    assert found and found[0]["side"] == "buy" and found[0]["status"] == "enter now"
    sane(found[0])


def test_flag_after_a_strong_move():
    closes = [1.1000] * 15 + [1.1000 + 0.0040 * i for i in range(1, 11)] + [1.1400, 1.1405, 1.1398, 1.1402, 1.1401]
    d1 = daily(closes, 0.0012)
    found = [x for x in playbook.daily_setups("EURUSD", EU, d1, hourly(1.1401)) if x["strategy"] == "Flag"]
    assert found and found[0]["side"] == "buy"
    sane(found[0])


def test_quiet_market_gives_no_daily_setups():
    closes = [1.1000 + 0.0003 * ((i % 3) - 1) for i in range(40)]
    found = playbook.daily_setups("EURUSD", EU, daily(closes, 0.003), hourly(1.1000))
    assert not [x for x in found if x["strategy"] in ("20-day breakout", "Flag", "RSI bounce")]


def test_rsi_matches_wilder_on_a_known_case():
    r = playbook.rsi([float(x) for x in range(1, 20)])          # only gains
    assert r[-1] == 100.0 and r[13] is None


def test_supply_demand_runs_on_a_trend_and_plans_with_it():
    import time as _t
    from test_topdown import trending_minutes
    from forexapp.strategy import topdown
    now = 1_795_000_000 // 60 * 60
    m1 = trending_minutes("EURUSD", now - 40 * 86400, 40, 0.000003)
    tfs = topdown.frames(m1, now)
    for p in playbook.supply_demand("EURUSD", EU, tfs["4h"], tfs["15m"]):
        assert p["side"] == "buy"
        sane(p)


def test_supply_demand_target_is_capped_at_3r():
    from test_topdown import trending_minutes
    from forexapp.strategy import topdown
    now = 1_795_000_000 // 60 * 60
    m1 = trending_minutes("EURUSD", now - 40 * 86400, 40, 0.000003)
    tfs = topdown.frames(m1, now)
    for p in playbook.supply_demand("EURUSD", EU, tfs["4h"], tfs["15m"]):
        assert p["plan"]["reward_risk"] <= 3.05
