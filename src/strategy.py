from __future__ import annotations

from typing import Any
from src.models import PreviousDayLevels, TradeSignal, Bar
from src.indicators import (
    sma, atr, rsi, avg_volume, vwap, ema, adx, avg_atr,
    is_price_near_sma, get_atr_based_levels, compute_multi_timeframe_trend
)


def _is_lunch_time(current_time, config) -> bool:
    """Check if current time is during lunch hours (11:30-13:00 ET)"""
    if not config.use_lunch_filter:
        return False
    market_time_str = current_time.strftime("%H:%M")
    return config.lunch_start <= market_time_str < config.lunch_end


def _is_market_open_delay(current_time, config) -> bool:
    """Check if we're within the market open delay window (first N minutes after 09:30)"""
    if not config.use_market_open_delay:
        return False
    market_time_str = current_time.strftime("%H:%M")
    # Market opens at 09:30 ET
    open_time = "09:30"
    try:
        current_min = int(market_time_str[:2]) * 60 + int(market_time_str[3:5])
        open_min = int(open_time[:2]) * 60 + int(open_time[3:5])
        return current_min - open_min < config.market_open_delay_minutes
    except Exception:
        return False


def _check_market_regime_filter(spy_bars, qqq_bars, config) -> bool:
    """
    Check market regime filter:
    - SPY > 200 EMA
    - SPY 20 EMA > 50 EMA
    - QQQ > VWAP
    - QQQ 8 SMA > 21 SMA
    """
    if not config.use_market_regime_filter:
        return True

    if not spy_bars or not qqq_bars:
        return True  # Skip if no data available

    spy_closes = [b.close for b in sorted(spy_bars, key=lambda b: b.timestamp)]
    qqq_closes = [b.close for b in sorted(qqq_bars, key=lambda b: b.timestamp)]

    # SPY > 200 EMA
    spy_ema_200 = ema(spy_closes, config.regime_spy_ema_200)
    spy_last = spy_closes[-1] if spy_closes else None
    if spy_ema_200 is not None and spy_last is not None:
        if spy_last <= spy_ema_200:
            return False

    # SPY 20 EMA > 50 EMA
    spy_ema_20 = ema(spy_closes, config.regime_spy_ema_20)
    spy_ema_50 = ema(spy_closes, config.regime_spy_ema_50)
    if spy_ema_20 is not None and spy_ema_50 is not None:
        if spy_ema_20 <= spy_ema_50:
            return False

    # QQQ > VWAP
    if config.regime_qqq_vwap_check:
        qqq_vwap = vwap(qqq_bars)
        qqq_last = qqq_closes[-1] if qqq_closes else None
        if qqq_vwap is not None and qqq_last is not None:
            if qqq_last <= qqq_vwap:
                return False

    # QQQ 8 SMA > 21 SMA
    qqq_sma_8 = sma(qqq_closes, config.regime_qqq_sma_8)
    qqq_sma_21 = sma(qqq_closes, config.regime_qqq_sma_21)
    if qqq_sma_8 is not None and qqq_sma_21 is not None:
        if qqq_sma_8 <= qqq_sma_21:
            return False

    return True


def _check_low_volatility_filter(bars, config) -> bool:
    """Check if current ATR is too low (current ATR < 0.8 * avg ATR)"""
    if not config.use_low_vol_filter:
        return True

    ordered = sorted(bars, key=lambda b: b.timestamp)
    current_atr = atr(ordered, config.atr_period)
    avg_atr_val = avg_atr(ordered, config.atr_period, lookback=20)

    if current_atr is not None and avg_atr_val is not None and avg_atr_val > 0:
        if current_atr < config.low_vol_atr_ratio * avg_atr_val:
            return False

    return True


def _check_adx_filter(bars, config) -> tuple[bool, float | None]:
    """Check ADX trend filter"""
    if not config.use_adx_filter:
        return True, None

    ordered = sorted(bars, key=lambda b: b.timestamp)
    adx_val, plus_di, minus_di = adx(ordered, config.adx_period)

    if adx_val is not None:
        return adx_val >= config.adx_threshold, adx_val

    return True, None  # Pass if insufficient data


def _check_confirmed_breakout(current_bar, breakout_price, config) -> bool:
    """Check if candle CLOSES above breakout level (confirmed breakout)"""
    if not config.require_confirmed_breakout:
        return True  # Original behavior: touch is enough

    return current_bar.close >= breakout_price


def build_signal(
    levels: PreviousDayLevels,
    recent_bars: list[Bar],
    state: dict[str, Any],
    config,
    regime_data: dict[str, Any] | None = None,
) -> TradeSignal | None:
    """
    Build trading signal based on strategy rules.

    Args:
        levels: Previous day levels
        recent_bars: Recent 1-minute bars for entry timeframe
        state: Per-symbol state dict with keys:
            - in_position (bool)
            - entry_price (float)
            - highest_price_since_entry (float)
            - _trend_bars (5Min bars for MTF)
            - _secondary_trend_bars (15Min bars for MTF)
        config: BotConfig with all strategy parameters
        regime_data: Dict with spy_bars, qqq_bars for market regime filter
    """
    if len(recent_bars) < config.slow_sma_period:
        return None

    ordered = sorted(recent_bars, key=lambda bar: bar.timestamp)
    closes = [bar.close for bar in ordered]
    current_bar = ordered[-1]
    price = current_bar.close
    current_time = current_bar.timestamp

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
    secondary_trend_bars = state.pop("_secondary_trend_bars", None) if "_secondary_trend_bars" in state else None

    # Time-based filters
    is_lunch = _is_lunch_time(current_time, config)
    is_open_delay = _is_market_open_delay(current_time, config)

    # Low volatility filter
    low_vol_ok = _check_low_volatility_filter(ordered, config)

    # ADX filter
    adx_ok, adx_val = _check_adx_filter(ordered, config)

    # Market regime filter (SPY/QQQ data passed via regime_data)
    regime_ok = True
    if config.use_market_regime_filter and regime_data:
        regime_ok = _check_market_regime_filter(
            regime_data.get("spy_bars"), regime_data.get("qqq_bars"), config
        )

    # ===========================================
    # EXIT LOGIC (if in position)
    # ===========================================
    if in_position and entry_price:
        entry = float(entry_price)
        high_water = max(float(highest_price or entry), price)
        state["highest_price_since_entry"] = high_water

        # Use ATR-based stops if enabled
        if config.use_atr_stops and atr_val is not None and atr_val > 0:
            base_stop_loss = config.atr_stop_loss_multiplier * atr_val
            base_take_profit = config.atr_take_profit_multiplier * atr_val
            trail_stop_dist = config.atr_trail_stop_multiplier * atr_val

            # Dynamic targets: if ADX > 35 (strong trend), extend take profit
            if config.use_dynamic_targets and adx_val is not None and adx_val > config.adx_strong_threshold:
                base_take_profit *= 1.5  # 50% larger target in strong trends

            stop_loss = entry - base_stop_loss
            take_profit = entry + base_take_profit
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

    # Skip entries during lunch
    if is_lunch:
        return None

    # Skip entries during market open delay
    if is_open_delay:
        return None

    # Skip if low volatility
    if not low_vol_ok:
        return None

    # Skip if ADX filter fails
    if not adx_ok:
        return None

    # Skip if market regime filter fails
    if not regime_ok:
        return None

    breakout_price = levels.high * (1.0 + config.breakout_buffer_pct)

    # Determine if we're in pre-market (before 10:30 ET) for adaptive volume
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
            config.slow_sma_period,
            secondary_trend_bars,
            config.secondary_trend_sma_period
        )
        mtf_ok = mtf.get("trend_up", True) and mtf.get("secondary_trend_up", True)

    # ===========================================
    # ENTRY STRATEGY 1: Breakout (Original + ATR-based + Confirmed)
    # ===========================================
    if config.use_breakout_entry:
        # ATR-based breakout if enabled
        if config.use_atr_breakout and atr_breakout_price is not None:
            # For ATR breakout, use confirmed breakout check
            breakout_condition = _check_confirmed_breakout(current_bar, atr_breakout_price, config)
        else:
            breakout_condition = _check_confirmed_breakout(current_bar, breakout_price, config)

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