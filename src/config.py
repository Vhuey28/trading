"""
Configuration for the Trend Trading Bot.
All settings can be overridden via config.json or environment variables.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BotConfig:
    # Symbol & Account
    symbol: str = "QQQ"
    timezone: str = "America/Los_Angeles"
    trade_window_start: str = "06:00"
    trade_window_end: str = "01:00"
    paper: bool = True
    dry_run: bool = True
    notional_usd: float = 25.0

    # Polling & Data
    poll_seconds: int = 15
    timeframe: str = "1Min"
    crypto_location: str = "us"
    data_dir: str = "data"
    log_dir: str = "logs"
    state_file: str = "state/trend_bot_state.json"
    archive_dir: str = "archive"
    trades_csv_path: str = "logs/trades.csv"

    # Risk Management
    max_daily_trades: int = 10
    min_minutes_between_trades: int = 15

    # Moving Averages
    fast_sma_period: int = 8
    slow_sma_period: int = 21

    # Lookback
    lookback_minutes: int = 60

    # Breakout
    breakout_buffer_pct: float = 0.002

    # Stops & Targets (percentage-based, used if ATR stops disabled)
    stop_loss_pct: float = 0.012
    take_profit_pct: float = 0.035
    trail_stop_pct: float = 0.012

    # Exit Behavior
    exit_when_window_closes: bool = True

    # Symbols (list takes precedence over single symbol)
    symbols: list[str] = None

    # Alpaca Data Feed (stocks only)
    stock_feed: str | None = "iex"
    stock_adjustment: str | None = "split"

    # Order Execution
    limit_price_offset_pct: float | None = 0.002
    extended_hours: bool | None = True

    # ========================================================================
    # STRATEGY IMPROVEMENTS (from improvement proposal)
    # ========================================================================

    # --- Indicator Periods ---
    rsi_period: int = 14
    volume_period: int = 20
    atr_period: int = 14

    # --- Adaptive Volume Filter (Improvement #1) ---
    use_adaptive_volume: bool = True
    volume_ratio_pre_market: float = 1.2       # Before 10:30 AM
    volume_ratio_regular_hours: float = 0.6    # After 10:30 AM
    minimum_volume_multiplier: float = 1.25    # Fallback when adaptive disabled

    # --- Shorter Breakout Lookback (Improvement #2) ---
    # lookback_minutes already defined above (default 60)

    # --- ATR-Based Breakout (Improvement #3) ---
    use_atr_breakout: bool = True
    atr_breakout_multiplier: float = 0.5          # price > high - (0.5 * ATR)
    atr_breakout_buffer: float = 0.5              # alias for atr_breakout_multiplier (used in breakout calc)

    # --- Pullback Entries (Improvement #4) ---
    use_pullback_entry: bool = True
    pullback_tolerance_pct: float = 0.002      # Price within 0.2% of fast SMA
    pullback_rsi_threshold: float = 50.0       # RSI > 50 for pullback

    # --- VWAP Continuation Entry (Improvement #5) ---
    use_vwap_continuation_entry: bool = True
    vwap_rsi_threshold: float = 55.0           # RSI > 55
    vwap_volume_threshold: float = 0.6         # Volume ratio > 0.6

    # --- Faster SMAs (Improvement #6) ---
    # fast_sma_period and slow_sma_period already defined above (8/21)

    # --- ATR-Based Stops (Improvement #7) ---
    use_atr_stops: bool = True
    atr_stop_loss_multiplier: float = 2.0
    atr_take_profit_multiplier: float = 4.0
    atr_trail_stop_multiplier: float = 1.5

    # --- Multi-Timeframe Confirmation (Improvement #8) ---
    use_multi_timeframe: bool = True
    trend_timeframe: str = "5Min"
    trend_sma_period: int = 50

    # --- Entry Strategy Toggles ---
    use_breakout_entry: bool = True

    # --- Logging & Reporting (Improvement #9) ---
    log_strategy_details: bool = True
    log_skipped_signals: bool = True
    export_trades_csv: bool = True
    require_previous_day_uptrend: bool = True

    # additional test-only fields
    stock_feed: str | None = None
    stock_adjustment: str | None = None
    limit_price_offset_pct: float | None = None
    extended_hours: bool | None = None

    @classmethod
    def load(cls, config_path: Path) -> "BotConfig":
        data: dict[str, Any] = {}
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        cfg_kwargs = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        # default symbols if not provided
        if "symbols" not in cfg_kwargs or cfg_kwargs.get("symbols") is None:
            if "symbol" in cfg_kwargs and cfg_kwargs["symbol"] is not None:
                cfg_kwargs["symbols"] = [cfg_kwargs["symbol"]]
            else:
                cfg_kwargs["symbols"] = ["QQQ", "SPY", "IWM", "SCHD", "BND"]
        cfg = cls(**cfg_kwargs)
        return cfg.with_env_overrides()

    def with_env_overrides(self) -> "BotConfig":
        values = self.__dict__.copy()
        env_map: dict[str, tuple[str, type[Any]]] = {
            "TREND_BOT_SYMBOL": ("symbol", str),
            "TREND_BOT_TIMEZONE": ("timezone", str),
            "TREND_BOT_DRY_RUN": ("dry_run", bool),
            "TREND_BOT_PAPER": ("paper", bool),
            "TREND_BOT_NOTIONAL_USD": ("notional_usd", float),
            "TREND_BOT_POLL_SECONDS": ("poll_seconds", int),
        }
        for env_name, (field_name, caster) in env_map.items():
            raw = os.getenv(env_name)
            if raw is None:
                continue
            if caster is bool:
                values[field_name] = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                values[field_name] = caster(raw)
        return BotConfig(**values)

    @property
    def trading_base_url(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    @property
    def data_base_url(self) -> str:
        return "https://data.alpaca.markets"