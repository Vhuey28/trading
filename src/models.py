from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PreviousDayLevels:
    day: date
    open: float
    high: float
    low: float
    close: float
    vwap: float
    trend: str


@dataclass(frozen=True)
class TradeSignal:
    action: str
    reason: str
    price: float
