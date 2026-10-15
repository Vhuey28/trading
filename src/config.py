"""
Configuration for the Trend Trading Bot.
All settings can be overridden via config.json or environment variables.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BotConfig:
    # Symbol & Account
    symbol: str = "QQQ"
    symbols: list[str] | None = None
    timezone: str = "America/Los_Angeles"
    trade_window_start: str = "06:00"
    trade_window_end: str = "01:00"
    paper: bool = True
    dry_run: bool = False
    notional_usd: float = 5000.0  # max-profit default: scales with account; use percent sizing to compound
    # Max-profit position sizing
    use_percent_position_sizing: bool = False  # if True, size = equity * percent_of_equity
    percent_of_equity: float = 0.20  # 20% of equity per trade (very aggressive; compounds)
    max_notional_usd: float = 20000.0  # hard cap per trade (margin-aware; raise with buying power)
    use_compounding: bool = True  # percent sizing automatically compounds as equity grows
    use_kelly_sizing: bool = False
    kelly_win_rate: float = 0.45  # fallback priors for Kelly
    kelly_avg_win: float = 0.037
    kelly_avg_loss: float = 0.012
    kelly_fraction: float = 0.25  # fractional Kelly (0.25 = quarter-Kelly)
    use_pyramiding: bool = False  # add to winning positions
    pyramid_add_pct: float = 0.5  # add 50% of initial size on trend continuation
    # Daily loss guard (very aggressive: 15% daily)
    max_daily_loss_pct: float = 0.15
    max_daily_loss_usd: float | None = None  # absolute cap, overrides pct if set
    use_daily_loss_halt: bool = True
    # Conviction-based sizing
    use_conviction_sizing: bool = True
    conviction_adx_threshold: float = 35.0
    conviction_volume_ratio: float = 1.5
    conviction_multiplier: float = 1.5  # multiply size when high conviction
    pullback_size_multiplier: float = 1.2  # pullback entries are higher win-rate (48% vs 33%)

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
    max_daily_trades: int = 900
    min_minutes_between_trades: int = 5

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

    # Indicator Periods
    rsi_period: int = 14
    volume_period: int = 20
    atr_period: int = 14

    # Alpaca Data Feed (stocks only)
    stock_feed: str | None = "iex"
    stock_adjustment: str | None = "split"

    # Order Execution
    limit_price_offset_pct: float | None = 0.002
    extended_hours: bool | None = True

    # =========================================================================
    # STRATEGY IMPROVEMENTS - All configurable
    # =========================================================================

    # --- Adaptive Volume Filter ---
    use_adaptive_volume: bool = True
    volume_ratio_pre_market: float = 1.2       # Before 10:30 AM ET
    volume_ratio_regular_hours: float = 0.8    # After 10:30 AM ET (increased from 0.6)
    minimum_volume_multiplier: float = 1.0     # Fallback when adaptive disabled

    # --- ATR-Based Breakout ---
    use_atr_breakout: bool = True
    atr_breakout_multiplier: float = 0.75      # Price > high - (0.75 * ATR) (increased from 0.5)
    require_confirmed_breakout: bool = True    # Require candle CLOSE above breakout level

    # --- Pullback Entries ---
    use_pullback_entry: bool = True
    pullback_tolerance_pct: float = 0.002      # Price within 0.2% of fast SMA
    pullback_rsi_threshold: float = 58.0       # RSI > 58 for pullback (increased from 50)

    # --- VWAP Continuation Entry ---
    use_vwap_continuation_entry: bool = True
    vwap_rsi_threshold: float = 60.0           # RSI > 60 (increased from 55)
    vwap_volume_threshold: float = 0.8         # Volume ratio > 0.8 (increased from 0.6)

    # --- ATR-Based Stops ---
    use_atr_stops: bool = True
    atr_stop_loss_multiplier: float = 1.5      # Stop = entry - 1.5 * ATR (was 2.0)
    atr_take_profit_multiplier: float = 3.0    # Target = entry + 3.0 * ATR (was 4.0)
    atr_trail_stop_multiplier: float = 2.0     # Trail = 2.0 * ATR (was 1.5)
    use_dynamic_targets: bool = True           # Allow larger targets when ADX > 35

    # --- Multi-Timeframe Confirmation ---
    use_multi_timeframe: bool = True
    trend_timeframe: str = "5Min"
    trend_sma_period: int = 50
    use_secondary_timeframe: bool = True       # 15Min confirmation
    secondary_trend_timeframe: str = "15Min"
    secondary_trend_sma_period: int = 50

    # --- Entry Strategy Toggles ---
    use_breakout_entry: bool = True

    # --- Low Volatility Filter ---
    use_low_vol_filter: bool = True
    low_vol_atr_ratio: float = 0.8             # Skip if current ATR < 0.8 * avg ATR

    # --- Lunch Hours Filter ---
    use_lunch_filter: bool = True
    lunch_start: str = "11:30"                 # ET
    lunch_end: str = "13:00"                   # ET

    # --- Market Open Delay ---
    use_market_open_delay: bool = True
    market_open_delay_minutes: int = 15        # Skip first 15 min after open

    # --- ATR Position Sizing ---
    use_atr_position_sizing: bool = True       # True = risk-based, False = fixed notional (max-profit: True)
    risk_per_trade_usd: float = 150.0          # Risk $150 per trade (aggressive; capped by max_notional)
    # When both percent and ATR sizing are enabled, percent takes precedence and ATR caps risk

    # --- Data Feed Preference ---
    prefer_sip_feed: bool = False              # Use SIP feed if available (requires subscription)

    # --- ADX Trend Filter ---
    use_adx_filter: bool = True
    adx_period: int = 14
    adx_threshold: float = 25.0                # ADX > 25 for trend
    adx_strong_threshold: float = 35.0         # ADX > 35 for strong trend (dynamic targets)

    # --- Market Regime Filter ---
    use_market_regime_filter: bool = True
    regime_spy_symbol: str = "SPY"
    regime_qqq_symbol: str = "QQQ"
    regime_spy_ema_200: int = 200
    regime_spy_ema_20: int = 20
    regime_spy_ema_50: int = 50
    regime_qqq_vwap_check: bool = True
    regime_qqq_sma_8: int = 8
    regime_qqq_sma_21: int = 21

    # --- Logging & Reporting ---
    require_previous_day_uptrend: bool = True
    log_strategy_details: bool = True
    log_skipped_signals: bool = True
    export_trades_csv: bool = True

    # --- Telegram Notifications ---
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # =========================================================================
    # HYBRID LEANER — Storage, Learner, Scanner (Level 3 RL + Level 2 Veto)
    # =========================================================================

    # --- Storage: SQLite trades + Parquet bars with 30-day rotation + gzipped archive ---
    storage_db_path: str = "logs/trades.db"
    storage_parquet_root: str = "data/parquet"
    storage_archive_root: str = "archive/parquet"
    storage_keep_days: int = 30  # keep only last 30d locally, archive older
    storage_gzipped_archive: bool = True  # also gzip for external drive
    storage_min_notional: float = 200.0  # floor for 1k equity (20% => $200)

    # --- Learner: Hybrid (RL Q-table + XGBoost veto) nightly retrain ---
    learner_enabled: bool = True
    learner_veto_threshold: float = 0.52  # win_prob < 0.52 => veto
    learner_retrain_hour: int = 22  # 22:00 ET after close
    learner_min_trades: int = 50  # need 50 trades to train
    learner_model_dir: str = "models"

    # --- Scanner: Open universe auto-rotation ---
    scanner_enabled: bool = True
    scanner_watchlist: list[str] | None = None  # None => DEFAULT_WATCHLIST (500 liquid)
    scanner_top_n: int = 5  # how many symbols to hold
    scanner_min_volume: int = 500000
    scanner_approval_required: bool = True  # ask via Telegram before rotating
    scanner_rank_cron_hour: int = 7  # 07:00 ET pre-market scan

    @classmethod
    def load(cls, config_path: Path) -> "BotConfig":
        logger = logging.getLogger("trend_bot")
        data: dict[str, Any] = {}
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))

        # Surface typos silently dropped by the field filter (e.g. the shipped
        # 'regimes_spy_ema_50' that never matched 'regime_spy_ema_50').
        unknown = [key for key in data if key not in cls.__dataclass_fields__]
        if unknown:
            logger.warning(
                "Ignoring unrecognized config.json key(s): %s (typo?)",
                ", ".join(sorted(unknown)),
            )

        cfg_kwargs = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        # default symbols if not provided - max-profit momentum mix
        if "symbols" not in cfg_kwargs or cfg_kwargs.get("symbols") is None:
            if "symbol" in cfg_kwargs and cfg_kwargs["symbol"] is not None:
                cfg_kwargs["symbols"] = [cfg_kwargs["symbol"]]
            else:
                cfg_kwargs["symbols"] = ["QQQ", "SPY", "AAPL", "NVDA", "TSLA"]
        cfg = cls(**cfg_kwargs)
        cfg = cfg.with_env_overrides()
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.notional_usd is None or self.notional_usd <= 0:
            raise ValueError(f"notional_usd must be > 0, got {self.notional_usd!r}")
        if not self.symbols:
            raise ValueError("symbols must be a non-empty list")
        if self.poll_seconds is not None and self.poll_seconds < 1:
            raise ValueError(f"poll_seconds must be >= 1, got {self.poll_seconds!r}")
        for name in ("stop_loss_pct", "take_profit_pct", "trail_stop_pct"):
            val = getattr(self, name)
            if val is not None and val <= 0:
                raise ValueError(f"{name} must be > 0, got {val!r}")
        if self.max_daily_trades is not None and self.max_daily_trades < 1:
            raise ValueError(f"max_daily_trades must be >= 1, got {self.max_daily_trades!r}")
        if self.percent_of_equity is not None and not (0 < self.percent_of_equity <= 1.0):
            raise ValueError(f"percent_of_equity must be (0,1], got {self.percent_of_equity!r}")
        if self.max_notional_usd is not None and self.max_notional_usd <= 0:
            raise ValueError(f"max_notional_usd must be > 0, got {self.max_notional_usd!r}")
        if self.max_daily_loss_pct is not None and not (0 < self.max_daily_loss_pct <= 1.0):
            raise ValueError(f"max_daily_loss_pct must be (0,1], got {self.max_daily_loss_pct!r}")

    def with_env_overrides(self) -> "BotConfig":
        values = self.__dict__.copy()
        env_map: dict[str, tuple[str, type[Any]]] = {
            "TREND_BOT_SYMBOL": ("symbol", str),
            "TREND_BOT_TIMEZONE": ("timezone", str),
            "TREND_BOT_DRY_RUN": ("dry_run", bool),
            "TREND_BOT_PAPER": ("paper", bool),
            "TREND_BOT_NOTIONAL_USD": ("notional_usd", float),
            "TREND_BOT_POLL_SECONDS": ("poll_seconds", int),
            "TELEGRAM_BOT_TOKEN": ("telegram_bot_token", str),
            "TELEGRAM_CHAT_ID": ("telegram_chat_id", str),
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

    # Convenience: get the effective data feed to use
    @property
    def effective_stock_feed(self) -> str:
        if self.prefer_sip_feed:
            return "sip"
        return self.stock_feed or "iex"