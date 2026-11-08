"""The risk check (trader-risk, ported to forex): PASS or BLOCK, with the reason.

A PASS only means the signal may be shown to the trader. It is not a yes,
and the app never places a trade. Checks, for a new entry:

| check       | blocks when |
| ----------- | ----------- |
| format      | no side, no lots, stop or target missing |
| stop        | the stop or the target is on the wrong side of the entry |
| loss        | lots x stop pips x pip value over the max loss per trade |
| reward:risk | (TP1 - entry) / (entry - stop) under the minimum |
| daily loss  | today's max losses at the stops plus this one over the daily limit |
| trades      | the max trades per day already taken |
| window      | outside the session's entry window, or in its quiet window |
| friday      | inside the last hour before the Friday close |
| news        | a high-impact event for either currency within the blackout |
| spread      | the current spread over 2x its normal level |
| duplicate   | the same pair and side already active |
| paused      | the monitor paused this pair and setup |
| market      | the market is closed |
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import instruments
from ..config import UserSettings
from ..timeutil import iso, market_open, minutes_to_week_close
from .news import blackout


@dataclass
class RiskResult:
    verdict: str
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons, "warnings": self.warnings, "numbers": self.numbers}


def check_order(order: dict, settings: UserSettings, now: float, *, today_entries: list = (),
                active: list = (), events: list = (), entry_window: tuple | None = None, quiet: bool = False,
                spread_now: float | None = None, spread_normal: float | None = None) -> RiskResult:
    """order: pair, side, entry, sl, tp1, lots, max_loss, setup_key (pair:session).
    today_entries: trades taken today, each with max_loss. active: signals open now (pair, side, id)."""
    reasons, warnings = [], []
    pair = order.get("pair")
    side = order.get("side")
    try:
        ins = instruments.get(pair or "")
    except KeyError:
        return RiskResult("BLOCK", ["format: unknown pair %r" % pair])
    lots = float(order.get("lots") or 0)
    entry, sl, tp = order.get("entry"), order.get("sl"), order.get("tp1")
    if side not in ("buy", "sell"):
        reasons.append("format: side must be buy or sell")
    if lots <= 0:
        reasons.append("format: zero lots (the stop is too far for your max loss, or under the minimum lot)")
    if entry is None or sl is None or tp is None:
        reasons.append("format: entry, stop and target are all required")
        return RiskResult("BLOCK", reasons)
    long = side == "buy"
    risk = (entry - sl) if long else (sl - entry)
    reward = (tp - entry) if long else (entry - tp)
    if risk <= 0:
        reasons.append("stop: the stop is not %s the entry" % ("below" if long else "above"))
    if reward <= 0:
        reasons.append("stop: the target is not %s the entry" % ("above" if long else "below"))
    max_loss = float(order.get("max_loss") or 0)
    if max_loss > settings.max_loss_per_trade + 1e-6:
        reasons.append("loss: %.2f lots x %.1f pips = %.2f %s at the stop, over your %.2f max loss per trade"
                       % (lots, ins.pips(max(risk, 0)), max_loss, settings.account_currency, settings.max_loss_per_trade))
    rr = reward / risk if risk > 0 else 0.0
    if rr + 1e-9 < settings.min_reward_risk:
        reasons.append("reward:risk %.2f is under your minimum %.2f" % (rr, settings.min_reward_risk))
    used = sum(float(e.get("max_loss") or 0) for e in today_entries)
    if used + max_loss > settings.max_daily_loss + 1e-6:
        reasons.append("daily loss: %.2f already at risk today + %.2f = %.2f, over your %.2f daily limit"
                       % (used, max_loss, used + max_loss, settings.max_daily_loss))
    if len(today_entries) >= settings.max_trades_per_day:
        reasons.append("trades: %d of %d trades already taken today" % (len(today_entries), settings.max_trades_per_day))
    if entry_window is not None and not (entry_window[0] <= now <= entry_window[1]):
        reasons.append("window: outside the session's entry window (%s to %s)" % (iso(entry_window[0]), iso(entry_window[1])))
    if quiet:
        reasons.append("window: inside the quiet window before scheduled data (08:20-08:35 New York)")
    if not market_open(now):
        reasons.append("market: the market is closed")
    elif minutes_to_week_close(now) <= settings.friday_cutoff_min:
        reasons.append("friday: no new trades in the last %d minutes before the weekly close" % settings.friday_cutoff_min)
    ev = blackout(events, pair, now, settings.news_blackout_before_min, settings.news_blackout_after_min)
    if ev is not None:
        reasons.append("news: %s %s at %s (blackout %d min before, %d after)"
                       % (ev.currency, ev.title, iso(ev.time), settings.news_blackout_before_min,
                          settings.news_blackout_after_min))
    if spread_now is not None and spread_normal:
        if spread_now > settings.spread_block_multiple * spread_normal:
            reasons.append("spread: %.1f pips is over %.1fx its normal %.1f pips"
                           % (ins.pips(spread_now), settings.spread_block_multiple, ins.pips(spread_normal)))
    elif spread_now is None:
        warnings.append("no live spread: the spread check was skipped")
    for a in active:
        if a.get("pair") == pair and a.get("side") == side and a.get("id") != order.get("id"):
            reasons.append("duplicate: a %s %s signal is already active" % (pair, side))
            break
    if order.get("setup_key") and order["setup_key"] in (settings.paused_setups or []):
        reasons.append("paused: %s was paused by the monitor after poor live results" % order["setup_key"])
    numbers = {"max_loss": round(max_loss, 2), "limit": settings.max_loss_per_trade, "reward_risk": round(rr, 2),
               "daily_used": round(used, 2), "daily_limit": settings.max_daily_loss,
               "trades_today": len(today_entries), "trades_limit": settings.max_trades_per_day}
    return RiskResult("BLOCK" if reasons else "PASS", reasons, warnings, numbers)
