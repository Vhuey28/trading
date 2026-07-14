from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import time as clock_time
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BotConfig:
    symbol: str = "QQQ"
    timezone: str = "America/Los_Angeles"
    trade_window_start: str = "06:00"
    trade_window_end: str = "01:00"
    paper: bool = True
    dry_run: bool = True
    notional_usd: float = 25.0
    poll_seconds: int = 60
    timeframe: str = "1Min"
    crypto_location: str = "us"
    data_dir: str = "data"
    log_dir: str = "logs"
    state_file: str = "state/trend_bot_state.json"
    max_daily_trades: int = 6
    min_minutes_between_trades: int = 30
    fast_sma_period: int = 20
    slow_sma_period: int = 50
    lookback_minutes: int = 180
    breakout_buffer_pct: float = 0.0015
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    trail_stop_pct: float = 0.015
    exit_when_window_closes: bool = True
    symbols: list[str] = None
    archive_dir: str = "archive"
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
