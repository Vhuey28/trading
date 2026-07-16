from __future__ import annotations

from typing import Any
from models import PreviousDayLevels, TradeSignal, Bar
from indicators import (
    sma, atr, rsi, avg_volume, vwap, is_price_near_sma,
    get_atr_based_levels, compute_multi_timeframe_trend
)


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

    # Calculate additional indicators
    atr_val = atr(ordered, config.atr_period)
    rsi_val = rsi(closes, config.rsi_period)
    avg_vol = avg_volume(ordered, config.volume_period)
    vol_ratio = None
    last_vol = ordered[-1].volume if ordered else None
    if avg_vol and last_vol is not None and avg_vol > 0:
        vol_ratio = last_vol / avg_vol

    # Calculate session VWAP (from market open)
    session_vwap = vwap(ordered)

    # ATR-based breakout level
    atr_breakout_price = None
    if config.use_atr_breakout and atr_val is not None and atr_val > 0:
        atr_breakout_price = levels.high - (config.atr_breakout_multiplier * atr_val)

    in_position = bool(state.get("in_position", False))
    entry_price = state.get("entry_price")
    highest_price = state.get("highest_price_since_entry")

    # Get trend bars for multi-timeframe (passed via state)
    trend_bars = state.pop("_trend_bars", None) if "_trend_bars" in state else None

    if in_position and entry_price:
        entry = float(entry_price)
        high_water = max(float(highest_price or entry), price)
        state["highest_price_since_entry"] = high_water

        # Use ATR-based stops if enabled
        if config.use_atr_stops and atr_val is not None and atr_val > 0:
            stop_loss = entry - (config.atr_stop_loss_multiplier * atr_val)
            take_profit = entry + (config.atr_take_profit_multiplier * atr_val)
            trail_stop_dist = config.atr_trail_stop_multiplier * atr_val
            trail_stop = high_water - trail_stop_dist
        else:
            stop_loss = entry * (1.0 - config.stop_loss_pct)
            take_profit = entry * (1.0 + config.take_profit_pct)
            trail_stop = high_water * (1.0 - config.trail_stop_pct)

        # Check exit conditions
        if price <= stop_loss:
            return TradeSignal("sell", "stop loss reached", price)
        if price >= take_profit:
            return TradeSignal("sell", "take profit reached", price)
        if price <= trail_stop:
            return TradeSignal("sell", "trailing stop reached", price)
        if fast < slow:
            return TradeSignal("sell", "moving average trend broke down", price)
        return None

    # ===========================================
    # ENTRY CONDITIONS (only if not in position)
    # ===========================================

    breakout_price = levels.high * (1.0 + config.breakout_buffer_pct)

    # Determine if we're in pre-market (before 10:30 ET) for adaptive volume
    current_time = ordered[-1].timestamp
    market_time_str = current_time.strftime("%H:%M")
    is_pre_market = market_time_str < "10:30"

    # Adaptive volume filter
    if config.use_adaptive_volume and vol_ratio is not None:
        if is_pre_market:
            volume_ok = vol_ratio >= config.volume_ratio_pre_market
        else:
            volume_ok = vol_ratio >= config.volume_ratio_regular_hours
    else:
        volume_ok = vol_ratio is not None and vol_ratio > config.minimum_volume_multiplier

    # Multi-timeframe trend confirmation (if enabled and trend_bars available)
    mtf_ok = True
    if config.use_multi_timeframe and trend_bars:
        mtf = compute_multi_timeframe_trend(
            trend_bars, ordered,
            config.trend_sma_period,
            config.fast_sma_period,
            config.slow_sma_period
        )
        mtf_ok = mtf.get("trend_up", True)

    # ===========================================
    # ENTRY STRATEGY 1: Breakout (Original + ATR-based)
    # ===========================================
    if config.use_breakout_entry:
        # ATR-based breakout if enabled
        if config.use_atr_breakout and atr_breakout_price is not None:
            breakout_condition = price > atr_breakout_price
        else:
            breakout_condition = price >= breakout_price

        if (config.require_previous_day_uptrend and levels.trend != "up"):
            pass  # Skip if previous day not up
        elif (fast > slow and
              price > fast and
              breakout_condition and
              volume_ok and
              mtf_ok):
            return TradeSignal("buy", "breakout entry", price)

    # ===========================================
    # ENTRY STRATEGY 2: Pullback Entry
    # ===========================================
    if config.use_pullback_entry:
        near_fast_sma = is_price_near_sma(price, fast, config.pullback_tolerance_pct)

        if (config.require_previous_day_uptrend and levels.trend != "up"):
            pass
        elif (fast > slow and
              near_fast_sma and
              rsi_val is not None and rsi_val > config.pullback_rsi_threshold and
              volume_ok and
              mtf_ok):
            return TradeSignal("buy", "pullback to fast SMA entry", price)

    # ===========================================
    # ENTRY STRATEGY 3: VWAP Continuation
    # ===========================================
    if config.use_vwap_continuation_entry:
        if session_vwap is not None:
            if (price > session_vwap and
                fast > slow and
                rsi_val is not None and rsi_val > config.vwap_rsi_threshold and
                vol_ratio is not None and vol_ratio > config.vwap_volume_threshold and
                mtf_ok):
                return TradeSignal("buy", "VWAP continuation entry", price)

    return None