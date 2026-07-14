from __future__ import annotations

from typing import Any, List
from models import Bar, PreviousDayLevels


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr(bars: list, period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def avg_volume(bars: list, period: int = 20) -> float | None:
    if len(bars) < period:
        return None
    vols = [max(0.0, b.volume) for b in bars[-period:]]
    return sum(vols) / len(vols)


def compute_previous_day_levels(day, bars: list[Bar]) -> PreviousDayLevels:
    if not bars:
        raise ValueError(f"No bars available for {day}")
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    total_volume = sum(max(bar.volume, 0.0) for bar in ordered)
    if total_volume > 0:
        vwap = sum(bar.close * max(bar.volume, 0.0) for bar in ordered) / total_volume
    else:
        vwap = sum(bar.close for bar in ordered) / len(ordered)

    open_price = ordered[0].open
    close_price = ordered[-1].close
    if close_price > open_price and close_price > vwap:
        trend = "up"
    elif close_price < open_price and close_price < vwap:
        trend = "down"
    else:
        trend = "flat"

    return PreviousDayLevels(
        day=day,
        open=open_price,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=close_price,
        vwap=vwap,
        trend=trend,
    )
