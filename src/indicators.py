from __future__ import annotations

from typing import Any, List
from models import Bar, PreviousDayLevels


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    """Exponential Moving Average"""
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for value in values[period:]:
        ema_val = (value - ema_val) * multiplier + ema_val
    return ema_val


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


def vwap(bars: list[Bar]) -> float | None:
    """Calculate VWAP (Volume Weighted Average Price) for a series of bars"""
    if not bars:
        return None
    total_volume = sum(max(bar.volume, 0.0) for bar in bars)
    if total_volume == 0:
        return sum(bar.close for bar in bars) / len(bars)
    return sum(bar.close * max(bar.volume, 0.0) for bar in bars) / total_volume


def vwap_from_open(bars: list[Bar]) -> float | None:
    """Calculate VWAP from market open (first bar of session)"""
    return vwap(bars)


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


def compute_session_vwap(bars: list[Bar], session_start_time: str = "09:30") -> float | None:
    """Calculate VWAP from session start (e.g., 09:30 for regular market hours)"""
    if not bars:
        return None
    # Filter bars from session start
    session_bars = [b for b in bars if b.timestamp.strftime("%H:%M") >= session_start_time]
    if not session_bars:
        return None
    return vwap(session_bars)


def is_price_near_sma(price: float, sma: float, tolerance_pct: float = 0.002) -> bool:
    """Check if price is within tolerance_pct of SMA (for pullback entries)"""
    if sma is None or price is None:
        return False
    return abs(price - sma) / sma <= tolerance_pct


def get_atr_based_levels(entry_price: float, atr_value: float, config) -> dict[str, float]:
    """Calculate ATR-based stop loss, take profit, and trailing stop levels"""
    if atr_value is None or atr_value <= 0:
        # Fallback to percentage-based
        return {
            "stop_loss": entry_price * (1.0 - config.stop_loss_pct),
            "take_profit": entry_price * (1.0 + config.take_profit_pct),
            "trail_stop": entry_price * (1.0 - config.trail_stop_pct),
        }

    return {
        "stop_loss": entry_price - (config.atr_stop_loss_multiplier * atr_value),
        "take_profit": entry_price + (config.atr_take_profit_multiplier * atr_value),
        "trail_stop": config.atr_trail_stop_multiplier * atr_value,  # Distance from high
    }


def compute_multi_timeframe_trend(
    trend_bars: list[Bar],
    entry_bars: list[Bar],
    trend_sma_period: int = 50,
    entry_sma_fast: int = 8,
    entry_sma_slow: int = 21
) -> dict[str, Any]:
    """Compute multi-timeframe trend confirmation
    Returns: dict with trend_up (bool), trend_fast_sma, trend_slow_sma, entry_fast_sma, entry_slow_sma
    """
    result = {
        "trend_up": False,
        "trend_fast_sma": None,
        "trend_slow_sma": None,
        "entry_fast_sma": None,
        "entry_slow_sma": None,
    }

    # Trend timeframe (e.g., 5Min) - check if in uptrend
    if trend_bars and len(trend_bars) >= trend_sma_period:
        trend_closes = [b.close for b in sorted(trend_bars, key=lambda b: b.timestamp)]
        result["trend_fast_sma"] = sma(trend_closes, min(20, len(trend_closes) // 2))
        result["trend_slow_sma"] = sma(trend_closes, trend_sma_period)
        if result["trend_fast_sma"] is not None and result["trend_slow_sma"] is not None:
            result["trend_up"] = result["trend_fast_sma"] > result["trend_slow_sma"]

    # Entry timeframe (e.g., 1Min) - get SMAs for entry logic
    if entry_bars and len(entry_bars) >= entry_sma_slow:
        entry_closes = [b.close for b in sorted(entry_bars, key=lambda b: b.timestamp)]
        result["entry_fast_sma"] = sma(entry_closes, entry_sma_fast)
        result["entry_slow_sma"] = sma(entry_closes, entry_sma_slow)

    return result