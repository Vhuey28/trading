# Trading Bot Improvement Proposal

## Goal

Increase the number of **high-quality trades** while maintaining sound
risk management. Avoid simply removing filters; instead add adaptive
logic.

## Recommended Configuration

``` python
poll_seconds = 15
max_daily_trades = 10
min_minutes_between_trades = 15

fast_sma_period = 8
slow_sma_period = 21

lookback_minutes = 60

breakout_buffer_pct = 0.002

stop_loss_pct = 0.012
take_profit_pct = 0.035
trail_stop_pct = 0.012
```

## Observations

Current logs show most symbols fail on: - Breakout - Volume

Trend, SMA alignment, and RSI frequently pass.

## Improvements

### 1. Adaptive Volume Filter

Current threshold appears too strict.

Use approximately:

``` python
if market_time < "10:30":
    volume_ratio >= 1.2
else:
    volume_ratio >= 0.6
```

### 2. Shorter Breakout Lookback

Reduce breakout window from 120--180 minutes to 45--60 minutes.

### 3. ATR-Based Breakout

Instead of requiring:

    price > breakout * (1 + breakout_buffer_pct)

use

    price > breakout - (0.5 * ATR)

### 4. Pullback Entries

Allow buys when:

-   Trend Up
-   Fast SMA \> Slow SMA
-   Price pulls back to Fast SMA
-   RSI \> 50

### 5. VWAP Continuation Entry

Allow entry if:

-   Price \> VWAP
-   Fast SMA \> Slow SMA
-   RSI \> 55
-   Volume ratio \> 0.6

even without a fresh breakout.

### 6. Faster SMAs

Use 8/21 or 9/20 instead of slower values.

### 7. ATR-Based Stops

Replace fixed percentages with:

    stop_loss = entry - (2 * ATR)
    take_profit = entry + (4 * ATR)
    trail_stop = 1.5 * ATR

### 8. Multi-Timeframe Confirmation

Require:

-   5-minute trend = Up
-   1-minute entry conditions

before buying.

### 9. Better Reporting

Backtest should record: - every BUY/SELL - skipped signals and reason -
equity curve - account balance - CSV export - drawdown - profit factor -
expectancy

### 10. Parameter Optimization

Add optimizer to test: - SMA periods - breakout lookback - volume
threshold - ATR multipliers - RSI thresholds

Choose robust settings across multiple date ranges rather than
optimizing for one period.

## Expected Outcome

-   More trade opportunities.
-   Better entries during trend continuations.
-   Fewer missed moves due to rigid breakout rules.
-   Stops and targets that adapt to volatility.
-   Stronger analytics for strategy evaluation.

Implement each feature behind configuration flags where practical to
allow A/B testing.
