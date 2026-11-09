"""Entry, stop loss and targets for TradingView, as a Pine Script you paste once.

TradingView has no way for another app to draw on your chart, so the app writes a
small indicator instead: Pine Editor -> paste -> Add to chart. It draws the trade
like TradingView's Long/Short Position tool: a green zone from the entry to the
target, a red zone from the entry to the stop, labelled lines at the entry, the
stop and both targets, from the signal's time to the end of its holding time.
It warns on the chart if it is opened on a different pair. It only draws: it
places nothing and sends nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import instruments
from .timeutil import parse_iso

TV_SYMBOLS = {"XAUUSD": "OANDA:XAUUSD"}


def tradingview_url(pair: str) -> str:
    return "https://www.tradingview.com/chart/?symbol=" + TV_SYMBOLS.get(pair, "OANDA:" + pair)


def _num(x: float, digits: int) -> str:
    return ("%." + str(digits) + "f") % float(x)


def _pine_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', "'").replace("\n", " ") + '"'


def pine_for_signal(sig: dict, hold_minutes: int = 120) -> str:
    """A Pine Script v6 indicator drawing this signal's entry, stop and targets."""
    pair = sig["pair"]
    ins = instruments.get(pair)
    d = ins.digits
    side = sig.get("side") or "buy"
    entry, sl, tp1 = sig["entry"], sig["sl"], sig["tp1"]
    tp2 = sig.get("tp2") or tp1
    if entry is None or sl is None or tp1 is None:
        raise ValueError("this signal has no entry, stop and target to draw")
    when = (sig.get("trigger") or {}).get("time") or sig.get("created_at")
    t0 = parse_iso(when) if when else int(datetime.now(timezone.utc).timestamp())
    dt = datetime.fromtimestamp(t0, timezone.utc)
    top_tp = max(tp1, tp2) if side == "buy" else min(tp1, tp2)
    title = "FX Desk: %s %s, %s" % (pair, side.upper(), sig.get("setup") or "setup")
    pips = lambda a, b: "%.1f" % (abs(a - b) / ins.pip)
    lines = [
        "//@version=6",
        "// Made by FX Desk for your own chart. It only draws the levels: it places no order.",
        "// TradingView: open Pine Editor, paste this, press Add to chart.",
        "indicator(%s, overlay = true, max_lines_count = 20, max_labels_count = 20, max_boxes_count = 4)" % _pine_str(title),
        "",
        't0 = timestamp("UTC", %d, %d, %d, %d, %d)' % (dt.year, dt.month, dt.day, dt.hour, dt.minute),
        "t1 = t0 + %d * 60 * 1000" % hold_minutes,
        "entry = %s" % _num(entry, d),
        "stop = %s" % _num(sl, d),
        "tp1 = %s" % _num(tp1, d),
        "tp2 = %s" % _num(tp2, d),
        "zoneTop = %s" % _num(top_tp, d),
        "",
        "if barstate.islast",
        "    box.new(t0, math.max(entry, zoneTop), t1, math.min(entry, zoneTop), xloc = xloc.bar_time, "
        "bgcolor = color.new(color.green, 85), border_color = color.new(color.green, 50))",
        "    box.new(t0, math.max(entry, stop), t1, math.min(entry, stop), xloc = xloc.bar_time, "
        "bgcolor = color.new(color.red, 85), border_color = color.new(color.red, 50))",
        "    line.new(t0, entry, t1, entry, xloc = xloc.bar_time, color = color.blue, width = 2)",
        "    line.new(t0, stop, t1, stop, xloc = xloc.bar_time, color = color.red, width = 2)",
        "    line.new(t0, tp1, t1, tp1, xloc = xloc.bar_time, color = color.green, width = 2)",
        "    line.new(t0, tp2, t1, tp2, xloc = xloc.bar_time, color = color.green, width = 1, style = line.style_dashed)",
        "    label.new(t1, entry, %s, xloc = xloc.bar_time, style = label.style_label_left, color = color.blue, "
        "textcolor = color.white, size = size.small)" % _pine_str("%s %s" % ("Buy" if side == "buy" else "Sell", _num(entry, d))),
        "    label.new(t1, stop, %s, xloc = xloc.bar_time, style = label.style_label_left, color = color.red, "
        "textcolor = color.white, size = size.small)" % _pine_str("SL %s (%s pips)" % (_num(sl, d), pips(entry, sl))),
        "    label.new(t1, tp1, %s, xloc = xloc.bar_time, style = label.style_label_left, color = color.green, "
        "textcolor = color.white, size = size.small)" % _pine_str("TP1 %s (%s pips)" % (_num(tp1, d), pips(entry, tp1))),
        "    label.new(t1, tp2, %s, xloc = xloc.bar_time, style = label.style_label_left, color = color.new(color.green, 30), "
        "textcolor = color.white, size = size.small)" % _pine_str("TP2 %s (%s pips)" % (_num(tp2, d), pips(entry, tp2))),
        "    if not str.contains(syminfo.ticker, %s)" % _pine_str(pair),
        "        label.new(bar_index, high, %s, color = color.orange, textcolor = color.black, "
        "style = label.style_label_down)" % _pine_str("These levels are for %s, not this chart" % pair),
        "",
    ]
    return "\n".join(lines)
