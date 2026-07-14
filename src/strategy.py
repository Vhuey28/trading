from __future__ import annotations

from typing import Any
from models import PreviousDayLevels, TradeSignal, Bar
from indicators import sma


def build_signal(
    levels: PreviousDayLevels,
    recent_bars: list[Bar],
    state: dict[str, Any],
    config,
) -> TradeSignal | None:
    if len(recent_bars) < config.slow_sma_period:
        return None

    ordered = sorted(recent_bars, key=lambda bar: bar.timestamp)
    closes = [bar.close for bar in ordered]
    price = closes[-1]
    fast = sma(closes, config.fast_sma_period)
    slow = sma(closes, config.slow_sma_period)
    if fast is None or slow is None:
        return None

    in_position = bool(state.get("in_position", False))
    entry_price = state.get("entry_price")
    highest_price = state.get("highest_price_since_entry")

    if in_position and entry_price:
        entry = float(entry_price)
        high_water = max(float(highest_price or entry), price)
        state["highest_price_since_entry"] = high_water

        if price <= entry * (1.0 - config.stop_loss_pct):
            return TradeSignal("sell", "stop loss reached", price)
        if price >= entry * (1.0 + config.take_profit_pct):
            return TradeSignal("sell", "take profit reached", price)
        if price <= high_water * (1.0 - config.trail_stop_pct):
            return TradeSignal("sell", "trailing stop reached", price)
        if fast < slow:
            return TradeSignal("sell", "moving average trend broke down", price)
        return None

    breakout_price = levels.high * (1.0 + config.breakout_buffer_pct)
    if levels.trend == "up" and fast > slow and price > fast and price >= breakout_price:
        return TradeSignal("buy", "previous-day uptrend breakout confirmed", price)
    return None
