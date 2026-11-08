"""Position size in lots (trader-budget, rewritten for forex).

The stop decides the size: lots = max loss / (stop distance in pips x pip
value per lot), rounded DOWN to the lot step. The size is the smallest of:

* max loss:  max_loss_per_trade / (stop pips x pip value + commission per lot)
* max lots:  the user's cap
* leverage:  account balance x max leverage / position value of one lot
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .. import instruments
from ..config import UserSettings


@dataclass
class Sizing:
    lots: float
    risk_pips: float
    pip_value_per_lot: float
    max_loss: float              # at the stop, with commission
    bound_by: str
    limits: dict
    notional: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def size_position(pair: str, entry: float, stop: float, settings: UserSettings, rates: dict,
                  commission_per_lot: float = 0.0, lot_step: float = 0.01, min_lot: float = 0.01) -> Sizing:
    ins = instruments.get(pair)
    rates = dict(rates)
    rates.setdefault(ins.symbol, entry)
    account = settings.account_currency
    risk_pips = abs(entry - stop) / ins.pip
    pv = instruments.pip_value_per_lot(pair, rates, account)
    per_lot_loss = risk_pips * pv + commission_per_lot
    notional_lot = instruments.notional_value(pair, 1.0, rates, account)
    limits = {
        "max loss": settings.max_loss_per_trade / per_lot_loss if per_lot_loss > 0 else 0.0,
        "max lots": settings.max_lots,
        "leverage": settings.account_balance * settings.max_leverage / notional_lot if notional_lot > 0 else 0.0,
    }
    bound_by = min(limits, key=limits.get)
    raw = limits[bound_by]
    lots = math.floor(raw / lot_step + 1e-9) * lot_step
    lots = round(lots, 2)
    if lots < min_lot:
        lots = 0.0
    return Sizing(lots, round(risk_pips, 1), round(pv, 4), round(lots * per_lot_loss, 2), bound_by,
                  {k: round(v, 2) for k, v in limits.items()}, round(lots * notional_lot, 2))
