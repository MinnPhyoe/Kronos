"""Pairs, pip sizes and pip values.

A standard lot is 100,000 units of the base currency (100 oz for gold).
Pip value per lot = pip size x contract size, in the quote currency, then
converted to the account currency with the latest prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    quote: str
    pip: float              # price move of one pip
    digits: int             # decimals quoted
    contract: float         # units per standard lot
    typical_spread_pips: float

    @property
    def point(self) -> float:
        return 10.0 ** -self.digits

    def pips(self, price_distance: float) -> float:
        return price_distance / self.pip

    def round_price(self, x: float) -> float:
        return round(x + 0.0, self.digits)

    def round_away(self, x: float, entry: float) -> float:
        """Round a stop to the quoted precision, away from the entry."""
        scale = 10 ** self.digits
        v = x * scale
        v = math.floor(v + 1e-9) if x < entry else math.ceil(v - 1e-9)
        return v / scale

    def round_toward(self, x: float, entry: float) -> float:
        """Round a target to the quoted precision, toward the entry (never promise more)."""
        scale = 10 ** self.digits
        v = x * scale
        v = math.floor(v + 1e-9) if x > entry else math.ceil(v - 1e-9)
        return v / scale


def _fx(sym, pip=0.0001, digits=5, spread=1.0):
    return Instrument(sym, sym[:3], sym[3:], pip, digits, 100_000.0, spread)


INSTRUMENTS = {
    "EURUSD": _fx("EURUSD", spread=0.8),
    "GBPUSD": _fx("GBPUSD", spread=1.0),
    "AUDUSD": _fx("AUDUSD", spread=0.9),
    "NZDUSD": _fx("NZDUSD", spread=1.3),
    "USDCAD": _fx("USDCAD", spread=1.3),
    "USDCHF": _fx("USDCHF", spread=1.2),
    "EURGBP": _fx("EURGBP", spread=1.1),
    "EURCHF": _fx("EURCHF", spread=1.5),
    "USDJPY": _fx("USDJPY", pip=0.01, digits=3, spread=0.9),
    "EURJPY": _fx("EURJPY", pip=0.01, digits=3, spread=1.4),
    "GBPJPY": _fx("GBPJPY", pip=0.01, digits=3, spread=2.0),
    "AUDJPY": _fx("AUDJPY", pip=0.01, digits=3, spread=1.6),
    # Gold: 1 lot = 100 oz, quoted to the cent; one pip = 0.10 (broker conventions differ: check yours).
    "XAUUSD": Instrument("XAUUSD", "XAU", "USD", 0.1, 2, 100.0, 3.0),
}


def get(symbol: str) -> Instrument:
    s = symbol.upper().replace("/", "").replace("_", "")
    if s not in INSTRUMENTS:
        raise KeyError("unknown pair %r; add it to forexapp/instruments.py" % symbol)
    return INSTRUMENTS[s]


def currencies(symbol: str) -> set:
    """Currencies whose news moves this pair (gold trades against USD news)."""
    ins = get(symbol)
    out = {ins.base, ins.quote}
    if "XAU" in out:
        out.add("USD")
    return out


def conversion_rate(ccy: str, account: str, rates: dict) -> float | None:
    """Units of ``account`` currency per one unit of ``ccy``, from the latest prices."""
    if ccy == account:
        return 1.0
    direct, inverse = ccy + account, account + ccy
    if rates.get(direct):
        return float(rates[direct])
    if rates.get(inverse):
        return 1.0 / float(rates[inverse])
    # one hop through USD
    if account != "USD" and ccy != "USD":
        a = conversion_rate(ccy, "USD", rates)
        b = conversion_rate("USD", account, rates)
        if a and b:
            return a * b
    return None


def pip_value_per_lot(symbol: str, rates: dict, account: str = "USD") -> float:
    """Value of one pip for one standard lot, in the account currency."""
    ins = get(symbol)
    in_quote = ins.pip * ins.contract
    rate = conversion_rate(ins.quote, account, rates)
    if rate is None:
        raise ValueError("no price to convert %s to %s; stream %s%s or %s%s too"
                         % (ins.quote, account, ins.quote, account, account, ins.quote))
    return in_quote * rate


def notional_value(symbol: str, lots: float, rates: dict, account: str = "USD") -> float:
    """Position size in the account currency (for the leverage cap)."""
    ins = get(symbol)
    rate = conversion_rate(ins.base, account, rates)
    if rate is None:
        # base -> quote at the pair price, then quote -> account
        px = rates.get(ins.symbol)
        q = conversion_rate(ins.quote, account, rates)
        if not px or q is None:
            raise ValueError("no price to value %s in %s" % (ins.base, account))
        rate = float(px) * q
    return lots * ins.contract * rate
