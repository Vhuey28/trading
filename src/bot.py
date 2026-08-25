from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import BotConfig
from src.logger import configure_logging, LOGGER_NAME
from src.alpaca_client import AlpacaClient
from src.indicators import compute_previous_day_levels, atr
from src.strategy import build_signal
from src.telegram_notifier import TelegramNotifier
from src.utils import (
    parse_alpaca_timestamp,
    is_trade_window_open,
    previous_data_day,
    send_macos_notification,
)
from src.models import Bar, PreviousDayLevels, TradeSignal


def symbol_file_stem(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


class TrendBot:
    def __init__(self, config: BotConfig, root_dir: Path) -> None:
        self.config = config
        self.root_dir = root_dir
        self.tz = ZoneInfo(config.timezone)
        self.data_dir = self.resolve_path(config.data_dir)
        self.state_file = self.resolve_path(config.state_file)
        self.logger = configure_logging(self.resolve_path(config.log_dir))
        self.client = AlpacaClient(config, self.logger)
        self.previous_levels: dict[str, PreviousDayLevels] = {}
        self.previous_levels_requested_day: dict[str, date] = {}
        self.last_notification_hour: int | None = None

        # Telegram notifier
        self.notifier = TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            logger=self.logger,
            enabled=config.telegram_enabled,
        )

        # CSV trade export
        self.trades_csv_path = self.resolve_path(config.trades_csv_path)
        if config.export_trades_csv:
            self._init_trades_csv()

        # Warn if using IEX feed with extended hours (IEX doesn't provide extended hours data)
        if config.extended_hours and getattr(config, "stock_feed", "iex") == "iex":
            self.logger.warning(
                "Config has extended_hours=true but stock_feed='iex'. "
                "IEX feed does NOT include extended hours data (pre-market/after-hours). "
                "Consider using stock_feed='sip' (requires subscription) or set extended_hours=false."
            )

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def _init_trades_csv(self) -> None:
        """Initialize trades CSV with headers if it doesn't exist"""
        if not self.trades_csv_path.exists():
            self.trades_csv_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trades_csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "symbol", "action", "price", "reason",
                    "entry_price", "exit_price", "pnl", "pnl_pct", "notional",
                    "fast_sma", "slow_sma", "rsi", "atr", "volume_ratio",
                    "session_vwap", "breakout_price",
                    # New analytics fields
                    "entry_reason", "exit_reason", "r_multiple", "atr_at_entry",
                    "adx_at_entry", "vwap_at_entry", "volume_ratio_at_entry",
                    "trade_duration_minutes", "hour_of_day", "day_of_week",
                    "strategy", "position_size", "risk_per_trade"
                ])

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt / partially-written state (e.g. crash or a stale file from
            # before atomic writes). Preserve it for inspection and start clean
            # so the bot can recover autonomously instead of failing forever.
            self.logger.error("State file %s is unreadable; starting from empty state", self.state_file)
            try:
                corrupt = self.state_file.with_suffix(self.state_file.suffix + ".corrupt")
                corrupt.write_text(self.state_file.read_text(encoding="utf-8", errors="replace"))
                self.logger.warning("Backed up corrupt state to %s", corrupt)
            except Exception:
                pass
            state = {}
        if "in_position" in state and "symbols" not in state:
            symbol = self.config.symbol
            state["symbols"] = {
                symbol: {
                    "in_position": state.pop("in_position", False),
                    "entry_price": state.pop("entry_price", None),
                    "highest_price_since_entry": state.pop("highest_price_since_entry", None),
                    "last_trade_at": state.pop("last_trade_at", None),
                    "last_signal_reason": state.pop("last_signal_reason", None),
                    "last_order_id": state.pop("last_order_id", None),
                }
            }
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then atomically rename.
        # A crash mid-write can never leave a truncated state file at the real
        # path — the reader sees either the old or the new complete file.
        tmp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.state_file)
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass

    def previous_day_cache_path(self, symbol: str, day: date) -> Path:
        stem = symbol_file_stem(symbol)
        return self.data_dir / f"{stem}_{day.isoformat()}_{self.config.timeframe}.csv"

    def _uses_stock_endpoint(self, symbol: str) -> bool:
        return "/" not in symbol

    def _fetch_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[Bar]:
        if self._uses_stock_endpoint(symbol):
            return self.client.fetch_stock_bars(symbol, start, end, timeframe)
        return self.client.fetch_crypto_bars(symbol, start, end, timeframe)

    def _fetch_bars_with_timeframe(self, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[Bar]:
        """Fetch bars with a specific timeframe (overrides config.timeframe)"""
        if self._uses_stock_endpoint(symbol):
            return self.client.fetch_stock_bars(symbol, start, end, timeframe)
        return self.client.fetch_crypto_bars(symbol, start, end, timeframe)

    def _fetch_regime_data(self, now: datetime) -> dict[str, Any]:
        """Fetch SPY and QQQ bars for market regime filter"""
        regime_data = {}
        try:
            # Need enough bars for 200 EMA + 50 EMA calculations
            lookback = max(self.config.regime_spy_ema_200 * 3, 500)
            end = now.astimezone(timezone.utc)
            start = end - timedelta(minutes=lookback)

            spy_bars = self._fetch_bars_with_timeframe(
                self.config.regime_spy_symbol, start, end, "1Min"
            )
            qqq_bars = self._fetch_bars_with_timeframe(
                self.config.regime_qqq_symbol, start, end, "1Min"
            )

            if spy_bars:
                regime_data["spy_bars"] = spy_bars
            if qqq_bars:
                regime_data["qqq_bars"] = qqq_bars

            self.logger.debug("Fetched regime data: SPY=%d bars, QQQ=%d bars",
                             len(spy_bars), len(qqq_bars))
        except Exception as e:
            self.logger.warning("Failed to fetch regime data: %s", e)
        return regime_data

    def _fetch_secondary_trend_bars(self, symbol: str, now: datetime) -> list[Bar]:
        """Fetch secondary timeframe bars (e.g., 15Min) for multi-timeframe confirmation"""
        if not self.config.use_secondary_timeframe:
            return []

        try:
            end = now.astimezone(timezone.utc)
            # Need enough bars for secondary_trend_sma_period
            lookback = max(self.config.secondary_trend_sma_period * 5, self.config.lookback_minutes * 5)
            start = end - timedelta(minutes=lookback)

            bars = self._fetch_bars_with_timeframe(
                symbol,
                start=start,
                end=end,
                timeframe=self.config.secondary_trend_timeframe,
            )
            self.logger.debug("[%s] Fetched %d secondary trend bars (%s)",
                             symbol, len(bars), self.config.secondary_trend_timeframe)
            return bars
        except Exception as e:
            self.logger.warning("[%s] Failed to fetch secondary trend bars (%s): %s",
                               symbol, self.config.secondary_trend_timeframe, e)
            return []

    def read_bars_csv(self, path: Path) -> list[Bar]:
        bars: list[Bar] = []
        text = path.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return bars

        # Try to detect delimiter using csv.Sniffer; fall back to tab or whitespace
        header = lines[0]
        reader = None
        try:
            sample = "\n".join(lines[:50])
            dialect = csv.Sniffer().sniff(sample)
            reader = csv.DictReader(lines, dialect=dialect)
        except Exception:
            if "\t" in header:
                reader = csv.DictReader(lines, delimiter="\t")
            elif "," in header:
                reader = csv.DictReader(lines, delimiter=",")
            else:
                # whitespace separated - parse manually
                cols = header.split()
                for ln in lines[1:]:
                    parts = ln.split()
                    if len(parts) < len(cols):
                        # skip malformed line
                        continue
                    row = dict(zip(cols, parts))
                    try:
                        bars.append(
                            Bar(
                                timestamp=parse_alpaca_timestamp(row.get("timestamp") or row.get("time") or row.get(cols[0])),
                                open=float(row.get("open") or row.get("o") or row.get(cols[1])),
                                high=float(row.get("high") or row.get("h") or row.get(cols[2])),
                                low=float(row.get("low") or row.get("l") or row.get(cols[3])),
                                close=float(row.get("close") or row.get("c") or row.get(cols[4])),
                                volume=float(row.get("volume") or row.get("v") or row.get(cols[5]) if len(cols) > 5 else 0.0),
                            )
                        )
                    except Exception:
                        continue
                return bars

        # Use csv.DictReader path
        for row in reader:
            try:
                bars.append(
                    Bar(
                        timestamp=parse_alpaca_timestamp(row.get("timestamp") or row.get("time") or next(iter(row.values()))),
                        open=float(row.get("open") or row.get("o") or row.get("Open") or 0.0),
                        high=float(row.get("high") or row.get("h") or 0.0),
                        low=float(row.get("low") or row.get("l") or 0.0),
                        close=float(row.get("close") or row.get("c") or 0.0),
                        volume=float(row.get("volume") or row.get("v") or 0.0),
                    )
                )
            except Exception:
                # skip malformed rows
                continue
        return bars

    def write_bars_csv(self, path: Path, bars: list[Bar]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
            )
            writer.writeheader()
            for bar in sorted(bars, key=lambda item: item.timestamp):
                writer.writerow(
                    {
                        "timestamp": bar.timestamp.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                )

    def get_previous_day_levels(self, symbol: str, now: datetime) -> PreviousDayLevels:
        requested_day = previous_data_day(now)
        if symbol in self.previous_levels and self.previous_levels_requested_day.get(symbol) == requested_day:
            return self.previous_levels[symbol]

        day = requested_day
        bars = None
        # Attempt to find the most recent trading day with bars, up to 10 days back
        for _ in range(10):
            cache_path = self.previous_day_cache_path(symbol, day)
            # prefer archive data when available (e.g. 1999-2017 historical files)
            archive_path = self.resolve_path(self.config.archive_dir) / f"{symbol_file_stem(symbol)}_{day.isoformat()}_{self.config.timeframe}.csv"
            if archive_path.exists():
                bars = self.read_bars_csv(archive_path)
                self.logger.info("[%s] Read %s previous-day bars from archive %s", symbol, len(bars), archive_path)
            elif cache_path.exists():
                bars = self.read_bars_csv(cache_path)
                self.logger.info("[%s] Read %s previous-day bars from %s", symbol, len(bars), cache_path)
            else:
                start_local = datetime.combine(day, clock_time.min, tzinfo=self.tz)
                end_local = start_local + timedelta(days=1)
                self.logger.info("[%s] Downloading previous-day bars for %s from Alpaca", symbol, day)
                try:
                    bars = self._fetch_bars(
                        symbol,
                        start=start_local,
                        end=end_local,
                        timeframe=self.config.timeframe,
                    )
                except Exception as e:
                    self.logger.warning("[%s] Failed to fetch bars for %s: %s", symbol, day, e)
                    bars = []

            if bars:
                if not cache_path.exists() and not archive_path.exists():
                    self.write_bars_csv(cache_path, bars)
                    self.logger.info("[%s] Saved %s previous-day bars to %s", symbol, len(bars), cache_path)
                break
            else:
                self.logger.info("[%s] No bars returned for %s, trying previous day...", symbol, day)
                # Go back one day, skipping weekends
                day = day - timedelta(days=1)
                while day.weekday() >= 5:
                    day -= timedelta(days=1)
        else:
            raise RuntimeError(f"No previous-day bars returned for {symbol} within 10 trading days of {requested_day}")

        levels = compute_previous_day_levels(day, bars)
        self.previous_levels[symbol] = levels
        self.previous_levels_requested_day[symbol] = requested_day
        self.logger.info(
            "[%s] Previous day %s (requested %s) trend=%s high=%.8f low=%.8f close=%.8f vwap=%.8f",
            symbol,
            day,
            requested_day,
            levels.trend,
            levels.high,
            levels.low,
            levels.close,
            levels.vwap,
        )
        return levels

    def fetch_recent_bars(self, symbol: str, now: datetime) -> list[Bar]:
        end = now.astimezone(timezone.utc)
        start = end - timedelta(minutes=self.config.lookback_minutes)
        bars = self._fetch_bars(
            symbol,
            start=start,
            end=end,
            timeframe=self.config.timeframe,
        )
        self.logger.debug(
            "[%s] fetch_recent_bars: requested %d minutes (%s to %s UTC), got %d bars (timeframe=%s)",
            symbol,
            self.config.lookback_minutes,
            start.isoformat(),
            end.isoformat(),
            len(bars),
            self.config.timeframe,
        )

        # Track the last successful fetch time to distinguish "no new bar yet" from failures
        state_key = "_last_successful_bars"
        state = getattr(self, state_key, {})
        last_bars = state.get(symbol, [])
        last_fetch_time = state.get(f"{symbol}_time")

        # If we got no new bars but had previous bars, check if enough time passed
        # for a new bar to form (based on timeframe). If not, reuse cached bars.
        if not bars and last_bars:
            timeframe_seconds = self._timeframe_to_seconds(self.config.timeframe)
            if last_fetch_time:
                time_since_last = (now - last_fetch_time).total_seconds()
                if time_since_last < timeframe_seconds * 0.8:
                    self.logger.debug(
                        "[%s] No new bars yet (%.1fs since last fetch, timeframe=%ds); reusing %d cached bars",
                        symbol,
                        time_since_last,
                        timeframe_seconds,
                        len(last_bars),
                    )
                    return last_bars
                self.logger.debug(
                    "[%s] Sufficient time passed (%.1fs) for new %s bar but API returned 0 bars",
                    symbol,
                    time_since_last,
                    self.config.timeframe,
                )

        if len(bars) >= self.config.slow_sma_period:
            # Update cache on successful fetch
            if not hasattr(self, state_key):
                setattr(self, state_key, {})
            getattr(self, state_key)[symbol] = bars
            getattr(self, state_key)[f"{symbol}_time"] = now
            return bars

        # Outside market hours or insufficient live data — fall back to the
        # most recent cached daily CSV so indicators can still be computed.
        stem = symbol_file_stem(symbol)
        candidates = sorted(
            self.data_dir.glob(f"{stem}_*_{self.config.timeframe}.csv"),
            reverse=True,
        )
        for csv_path in candidates:
            cached = self.read_bars_csv(csv_path)
            if len(cached) >= self.config.slow_sma_period:
                self.logger.warning(
                    "[%s] Live bars insufficient (%d bars); using cached %s as fallback",
                    symbol,
                    len(bars),
                    csv_path.name,
                )
                return cached

        # If we have some cached bars (even if less than slow_sma_period), use them
        if last_bars:
            self.logger.warning(
                "[%s] Live bars insufficient (%d bars); using %d previously cached bars as fallback",
                symbol,
                len(bars),
                len(last_bars),
            )
            return last_bars

        # Return whatever we have (possibly empty — caller handles it)
        if not bars:
            self.logger.warning(
                "[%s] Live bars insufficient (%d bars); no cached fallback available with enough data",
                symbol,
                len(bars),
            )
        return bars

    def _timeframe_to_seconds(self, timeframe: str) -> int:
        """Convert timeframe string (e.g., '1Min', '5Min', '1Hour') to seconds."""
        timeframe = timeframe.lower().strip()
        if timeframe.endswith("min"):
            return int(timeframe[:-3]) * 60
        if timeframe.endswith("hour") or timeframe.endswith("h"):
            return int(timeframe[:-4]) * 3600 if timeframe.endswith("hour") else int(timeframe[:-1]) * 3600
        if timeframe.endswith("day") or timeframe.endswith("d"):
            return int(timeframe[:-3]) * 86400 if timeframe.endswith("day") else int(timeframe[:-1]) * 86400
        return 60  # default to 1 minute

    def fetch_trend_bars(self, symbol: str, now: datetime) -> list[Bar]:
        """Fetch higher timeframe bars for multi-timeframe trend confirmation"""
        if not self.config.use_multi_timeframe:
            return []

        end = now.astimezone(timezone.utc)
        # Need enough bars for trend_sma_period (default 50)
        lookback = max(self.config.trend_sma_period * 5, self.config.lookback_minutes * 5)
        start = end - timedelta(minutes=lookback)

        self.logger.debug(
            "[%s] fetch_trend_bars: requested lookback=%d min (%s to %s UTC), timeframe=%s",
            symbol,
            lookback,
            start.isoformat(),
            end.isoformat(),
            self.config.trend_timeframe,
        )

        try:
            bars = self._fetch_bars_with_timeframe(
                symbol,
                start=start,
                end=end,
                timeframe=self.config.trend_timeframe,
            )
            self.logger.debug(
                "[%s] fetch_trend_bars: got %d bars (timeframe=%s)",
                symbol,
                len(bars),
                self.config.trend_timeframe,
            )
            return bars
        except Exception as e:
            self.logger.warning("[%s] Failed to fetch trend bars (%s): %s", symbol, self.config.trend_timeframe, e)
            return []

    @staticmethod
    def _order_fill_price(order: dict[str, Any] | None) -> float | None:
        """Best-effort broker fill price; returns the first present fill field or None."""
        if isinstance(order, dict):
            fill = order.get("avg_fill_price") or order.get("filled_avg_price")
            if fill:
                try:
                    return float(fill)
                except (TypeError, ValueError):
                    return None
        return None

    def sync_live_position(self, symbol: str, symbol_state: dict[str, Any]) -> None:
        if self.config.dry_run:
            return
        position = self.client.get_position(symbol)
        symbol_state["in_position"] = bool(position)
        if position and not symbol_state.get("entry_price"):
            avg_entry = position.get("avg_entry_price")
            if avg_entry is not None:
                symbol_state["entry_price"] = float(avg_entry)

    def can_trade_now(self, state: dict[str, Any], symbol: str, now: datetime) -> bool:
        today_key = now.date().isoformat()
        if state.get("trade_count_day") != today_key:
            state["trade_count_day"] = today_key
            state["trade_count"] = 0
        if int(state.get("trade_count", 0)) >= self.config.max_daily_trades:
            return False

        symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
        last_trade_at = symbol_state.get("last_trade_at")
        if not last_trade_at:
            return True
        last = parse_alpaca_timestamp(last_trade_at).astimezone(self.tz)
        next_allowed = last + timedelta(minutes=self.config.min_minutes_between_trades)
        return now >= next_allowed

    def record_trade(self, state: dict[str, Any], symbol: str, signal: TradeSignal, order: dict[str, Any] | None, notional: float | None = None) -> None:
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state["last_trade_at"] = now_str
        state["trade_count"] = int(state.get("trade_count", 0)) + 1

        symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
        symbol_state["last_trade_at"] = now_str
        symbol_state["last_signal_reason"] = signal.reason
        symbol_state["last_order_id"] = order.get("id") if order else None
        if signal.action == "buy":
            symbol_state["in_position"] = True
            # Prefer the broker's reported fill price; fall back to the signal
            # price (last bar close) when the response has no fill yet.
            fill = self._order_fill_price(order)
            entry = float(fill) if fill is not None else float(signal.price)
            symbol_state["entry_price"] = entry
            filled_qty = None
            if isinstance(order, dict):
                fq = order.get("filled_qty")
                if fq:
                    try:
                        filled_qty = float(fq)
                    except (TypeError, ValueError):
                        filled_qty = None
            # Persist the executed qty so exit P&L uses the real size, including
            # when ATR position sizing was applied.
            symbol_state["entry_qty"] = filled_qty or ((notional if notional is not None else self.config.notional_usd) / entry)
            symbol_state["highest_price_since_entry"] = entry
        elif signal.action == "sell":
            symbol_state["in_position"] = False
            symbol_state["entry_price"] = None
            symbol_state["highest_price_since_entry"] = None
            symbol_state.pop("entry_qty", None)

    def export_trade_to_csv(
        self,
        symbol: str,
        signal: TradeSignal,
        order: dict[str, Any] | None,
        entry_price: float | None,
        exit_price: float | None,
        pnl: float | None,
        pnl_pct: float | None,
        indicators: dict[str, Any]
    ) -> None:
        """Export trade to CSV for analysis"""
        if not self.config.export_trades_csv:
            return

        # Calculate additional analytics
        r_multiple = ""
        if pnl is not None and entry_price is not None and indicators.get("atr") is not None:
            atr_at_entry = indicators.get("atr")
            risk_atr = atr_at_entry * self.config.atr_stop_loss_multiplier if self.config.use_atr_stops else entry_price * self.config.stop_loss_pct
            if risk_atr > 0:
                r_multiple = round(pnl / risk_atr, 2)

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.trades_csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            # Determine strategy name from reason
            reason = signal.reason.lower()
            if "breakout" in reason:
                strategy = "breakout"
            elif "pullback" in reason:
                strategy = "pullback"
            elif "vwap" in reason:
                strategy = "vwap_continuation"
            else:
                strategy = "unknown"

            # Calculate trade duration for exits
            trade_duration_minutes = ""
            hour_of_day = ""
            day_of_week = ""
            if signal.action == "sell" and entry_price is not None:
                # Use current time as exit time
                exit_time = datetime.now(self.tz)
                # We'd need entry_time - for now leave blank as we don't track it in state
                hour_of_day = exit_time.hour
                day_of_week = exit_time.strftime("%A")

            writer.writerow([
                now,
                symbol,
                signal.action,
                signal.price,
                signal.reason,
                entry_price if entry_price is not None else "",
                exit_price if exit_price is not None else "",
                pnl if pnl is not None else "",
                pnl_pct if pnl_pct is not None else "",
                self.config.notional_usd,
                indicators.get("fast_sma", ""),
                indicators.get("slow_sma", ""),
                indicators.get("rsi", ""),
                indicators.get("atr", ""),
                indicators.get("volume_ratio", ""),
                indicators.get("session_vwap", ""),
                indicators.get("breakout_price", ""),
                # New analytics fields
                signal.reason if signal.action == "buy" else "",  # entry_reason
                signal.reason if signal.action == "sell" else "",  # exit_reason
                r_multiple,
                indicators.get("atr", ""),
                "",  # adx_at_entry - would need to pass from strategy
                indicators.get("session_vwap", ""),
                indicators.get("volume_ratio", ""),
                trade_duration_minutes,
                hour_of_day,
                day_of_week,
                strategy,
                self.config.notional_usd,  # position_size
                self.config.risk_per_trade_usd if self.config.use_atr_position_sizing else ""
            ])

    def execute_signal(self, symbol: str, signal: TradeSignal, state: dict[str, Any], indicators: dict[str, Any] | None = None) -> None:
        if signal.action == "buy" and not self.can_trade_now(state, symbol, datetime.now(self.tz)):
            self.logger.info("[%s] Signal skipped because trade limits are active: %s", symbol, signal.reason)
            if self.config.log_skipped_signals:
                self.logger.info("[%s] SKIPPED BUY: %s (trade limits)", symbol, signal.reason)
            return

        if self.config.dry_run:
            self.logger.info(
                "[%s] DRY RUN: would %s at %.8f because %s",
                symbol,
                signal.action,
                signal.price,
                signal.reason,
            )
            self.record_trade(state, symbol, signal, None)
            # Export dry run trades too for analysis
            if self.config.export_trades_csv and indicators:
                self.export_trade_to_csv(
                    symbol, signal, None,
                    signal.price if signal.action == "buy" else None,
                    signal.price if signal.action == "sell" else None,
                    None, None, indicators
                )
            return

        # Calculate position size (ATR-based or fixed notional)
        notional = self.config.notional_usd
        if signal.action == "buy" and self.config.use_atr_position_sizing:
            atr_val = indicators.get("atr") if indicators else None
            if atr_val and atr_val > 0:
                stop_distance = self.config.atr_stop_loss_multiplier * atr_val
                if stop_distance > 0:
                    # Risk per trade = notional / stop_distance * price
                    notional = self.config.risk_per_trade_usd / stop_distance * signal.price
                    # Cap at configured notional
                    notional = min(notional, self.config.notional_usd)
                    self.logger.info("[%s] ATR position sizing: atr=%.4f, stop_dist=%.4f, notional=%.2f",
                                     symbol, atr_val, stop_distance, notional)

        entry_price = None
        exit_price = None
        pnl = None
        pnl_pct = None

        # Best-effort real fill data from the broker; falls back to the signal
        # (bar close) price when the immediate response has no fill fields yet.
        def _fill_price(order: dict[str, Any] | None, default: float | None) -> float | None:
            return self._order_fill_price(order) or default

        def _order_rejected(order: dict[str, Any] | None) -> bool:
            if not isinstance(order, dict):
                return True  # None response = order was definitely not accepted
            status = (order.get("status") or "").lower()
            return status in {"rejected", "canceled", "expired"}

        if signal.action == "buy":
            order = self.client.place_market_buy(symbol, notional)
            if _order_rejected(order):
                reason = (order or {}).get("status", "no response")
                self.logger.error("[%s] BUY order was NOT accepted (status=%s); not recording a position.", symbol, reason)
                self.notifier.error(f"order_{symbol}", f"BUY rejected (status={reason})")
                return
            entry_price = _fill_price(order, signal.price)
            # Telegram notification
            self.notifier.trade_opened(symbol, entry_price, signal.reason)
        elif signal.action == "sell":
            order = self.client.close_position(symbol)
            if order is None:
                # Ambiguous: we cannot confirm the close. Keep the position marked
                # open rather than believing we're flat when the position may still
                # exist, so the loop does not blindly re-enter on top of it.
                self.logger.error("[%s] SELL close returned no confirmation; NOT clearing position.", symbol)
                self.notifier.error(f"order_{symbol}", "SELL close returned no confirmation; position not cleared")
                return
            exit_price = _fill_price(order, signal.price)
            # Calculate P&L from the persisted entry and the real qty if known
            symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
            entry = symbol_state.get("entry_price")
            if entry:
                entry = float(entry)
                qty = symbol_state.get("entry_qty") or (notional / entry)
                pnl = (exit_price - entry) * float(qty)
                pnl_pct = (exit_price / entry - 1.0) * 100.0
                # Telegram notification
                self.notifier.trade_closed(symbol, entry, exit_price, pnl, signal.reason)
        else:
            order = None

        self.logger.info("[%s] Placed %s order: %s", symbol, signal.action, order if order is not None else "NONE")
        self.record_trade(state, symbol, signal, order, notional)

        # Export trade to CSV
        if self.config.export_trades_csv and indicators:
            self.export_trade_to_csv(
                symbol, signal, order,
                entry_price, exit_price, pnl, pnl_pct, indicators
            )

    def run_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.tz)
        now = now.astimezone(self.tz)

        state = self.load_state()
        state.setdefault("trade_count_day", now.date().isoformat())
        state.setdefault("trade_count", 0)

        window_open = is_trade_window_open(
            now,
            self.config.trade_window_start,
            self.config.trade_window_end,
        )

        # Check market clock from Alpaca to know if market is actually open
        # (includes extended hours if configured)
        if self.config.extended_hours:
            market_open = self.client.is_market_open_extended()
        else:
            market_open = self.client.is_market_open()
        self.logger.debug(
            "Market clock check: window_open=%s, market_open=%s, now=%s",
            window_open,
            market_open,
            now.isoformat(),
        )

        if not window_open:
            exited_any = False
            for symbol in self.config.symbols:
                symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
                self.sync_live_position(symbol, symbol_state)
                if symbol_state.get("in_position") and self.config.exit_when_window_closes:
                    signal = TradeSignal("sell", "configured trading window is closed", float(symbol_state["entry_price"]))
                    self.execute_signal(symbol, signal, state)
                    exited_any = True

            if not exited_any:
                self.logger.info("Outside trading window; no trade check.")
            self._send_hourly_notification(now, "Trend bot is alive but outside trading hours.")
            self.save_state(state)
            return

        # If market is closed (e.g., weekend/holiday) but our window is open,
        # log a clear message and skip trading to avoid using stale data
        if not market_open:
            self.logger.warning(
                "Trading window is open but market is CLOSED (weekend/holiday). Skipping trade checks to avoid stale data."
            )
            self._send_hourly_notification(now, "Market closed (weekend/holiday); bot waiting.")
            self.save_state(state)
            return

        # Cache for storing previous bars to handle "no new bar yet" scenarios
        if not hasattr(self, "_bar_cache"):
            self._bar_cache = {}

        # Pre-fetch regime data (SPY/QQQ) if market regime filter is enabled
        regime_data = {}
        if self.config.use_market_regime_filter:
            regime_data = self._fetch_regime_data(now)

        for symbol in self.config.symbols:
            symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
            self.sync_live_position(symbol, symbol_state)

            try:
                levels = self.get_previous_day_levels(symbol, now)
            except Exception as e:
                self.logger.error("[%s] Failed to get previous-day levels: %s", symbol, e)
                self.notifier.error(f"prev_day_levels:{symbol}", f"{type(e).__name__}: {e}")
                continue

            recent_bars = self.fetch_recent_bars(symbol, now)

            # If no live bars returned, try to use cached bars from previous successful fetch
            if not recent_bars:
                self.logger.warning("[%s] No live bars returned; checking cache...", symbol)
                cached_bars = self._bar_cache.get(symbol)
                if cached_bars:
                    self.logger.info(
                        "[%s] Using %d cached bars from previous successful fetch (timestamp: %s)",
                        symbol,
                        len(cached_bars),
                        cached_bars[-1].timestamp.isoformat() if cached_bars else "N/A",
                    )
                    recent_bars = cached_bars
                else:
                    self.logger.warning("[%s] No cached bars available either; skipping this cycle", symbol)
                    continue
            else:
                # Update cache with successful fetch
                self._bar_cache[symbol] = recent_bars

            if not recent_bars:
                self.logger.warning("[%s] No bars available (live or cached); skipping", symbol)
                continue

            # Fetch trend timeframe bars for multi-timeframe confirmation
            trend_bars = []
            secondary_trend_bars = []
            if self.config.use_multi_timeframe:
                trend_bars = self.fetch_trend_bars(symbol, now)
                # Fetch secondary timeframe (e.g., 15Min)
                if self.config.use_secondary_timeframe:
                    secondary_trend_bars = self._fetch_secondary_trend_bars(symbol, now)

            # compute indicators for logging and strategy
            ordered = sorted(recent_bars, key=lambda bar: bar.timestamp)
            closes = [bar.close for bar in ordered]
            price = closes[-1] if closes else None
            current_bar = ordered[-1] if ordered else None
            current_time = current_bar.timestamp if current_bar else now
            from src.indicators import sma, atr, rsi, avg_volume, vwap

            fast = sma(closes, self.config.fast_sma_period)
            slow = sma(closes, self.config.slow_sma_period)
            atr_val = atr(ordered, self.config.atr_period)
            rsi_val = rsi(closes, self.config.rsi_period)
            avg_vol = avg_volume(ordered, self.config.volume_period)
            vol_ratio = None
            last_vol = ordered[-1].volume if ordered else None
            if avg_vol and last_vol is not None and avg_vol > 0:
                vol_ratio = last_vol / avg_vol

            session_vwap = vwap(ordered)

            breakout_price = levels.high * (1.0 + self.config.breakout_buffer_pct)
            atr_breakout_price = None
            if self.config.use_atr_breakout and atr_val is not None:
                atr_breakout_price = levels.high - (self.config.atr_breakout_multiplier * atr_val)

            # Check individual conditions for detailed logging
            market_time_str = current_time.strftime("%H:%M")
            is_pre_market = market_time_str < "10:30"

            # Volume check
            if self.config.use_adaptive_volume and vol_ratio is not None:
                if is_pre_market:
                    volume_ok = vol_ratio >= self.config.volume_ratio_pre_market
                else:
                    volume_ok = vol_ratio >= self.config.volume_ratio_regular_hours
            else:
                volume_ok = vol_ratio is not None and vol_ratio > self.config.minimum_volume_multiplier

            # Multi-timeframe check
            mtf_ok = True
            if self.config.use_multi_timeframe and trend_bars:
                from src.indicators import compute_multi_timeframe_trend
                mtf = compute_multi_timeframe_trend(
                    trend_bars, ordered,
                    self.config.trend_sma_period,
                    self.config.fast_sma_period,
                    self.config.slow_sma_period,
                    secondary_trend_bars,
                    self.config.secondary_trend_sma_period
                )
                mtf_ok = mtf.get("trend_up", True) and mtf.get("secondary_trend_up", True)

            checks = {
                "Trend Up": levels.trend == "up",
                "Fast SMA > Slow SMA": (fast is not None and slow is not None and fast > slow),
                "Price > Fast SMA": (price is not None and fast is not None and price > fast),
                "Breakout": (price is not None and price >= breakout_price),
                "ATR Breakout": (price is not None and atr_breakout_price is not None and price > atr_breakout_price),
                "Volume": volume_ok,
                "RSI": (rsi_val is not None and 40 <= rsi_val <= 70),
                "MTF Trend": mtf_ok,
            }

            # formatted block log
            lines = []
            lines.append("=" * 41)
            lines.append(f"{symbol}")
            lines.append("")
            lines.append(f"Price            {price:.2f}" if price is not None else "Price            n/a")
            lines.append(f"Fast SMA         {fast:.2f}" if fast is not None else "Fast SMA         n/a")
            lines.append(f"Slow SMA         {slow:.2f}" if slow is not None else "Slow SMA         n/a")
            lines.append(f"ATR              {atr_val:.2f}" if atr_val is not None else "ATR              n/a")
            lines.append(f"RSI              {rsi_val:.1f}" if rsi_val is not None else "RSI              n/a")
            lines.append(f"Volume Ratio     {vol_ratio:.2f}" if vol_ratio is not None else "Volume Ratio     n/a")
            lines.append(f"Session VWAP     {session_vwap:.2f}" if session_vwap is not None else "Session VWAP     n/a")
            lines.append(f"Breakout         {breakout_price:.2f}")
            if atr_breakout_price is not None:
                lines.append(f"ATR Breakout     {atr_breakout_price:.2f}")
            lines.append("")
            for name, ok in checks.items():
                lines.append(("✓ " if ok else "✗ ") + name)
            lines.append("")

            # Build signal with trend_bars for multi-timeframe and regime_data.
            # build_signal mutates the working copy, so afterwards we persist the
            # trailing high-water mark it ratcheted back onto the real state —
            # otherwise the trailing stop would reset to entry/current price each
            # poll and never lock in session gains (and live would diverge from
            # the backtest, which passes the real object).
            symbol_state_with_mtf = dict(symbol_state)
            symbol_state_with_mtf["_trend_bars"] = trend_bars
            symbol_state_with_mtf["_secondary_trend_bars"] = secondary_trend_bars
            signal = build_signal(levels, recent_bars, symbol_state_with_mtf, self.config, regime_data)
            if symbol_state.get("in_position") and symbol_state_with_mtf.get("highest_price_since_entry") is not None:
                symbol_state["highest_price_since_entry"] = symbol_state_with_mtf["highest_price_since_entry"]
            if signal:
                lines.append(f"{signal.action.title()} - {signal.reason}")
                self.logger.info("\n" + "\n".join(lines) + "\n" + "=" * 41)
                indicators = {
                    "fast_sma": fast,
                    "slow_sma": slow,
                    "rsi": rsi_val,
                    "atr": atr_val,
                    "volume_ratio": vol_ratio,
                    "session_vwap": session_vwap,
                    "breakout_price": breakout_price,
                }
                self.execute_signal(symbol, signal, state, indicators)
            else:
                lines.append("No Trade")
                if self.config.log_skipped_signals and self.config.log_strategy_details:
                    # Log which conditions failed
                    failed = [name for name, ok in checks.items() if not ok]
                    if failed:
                        self.logger.info("[%s] Skipped: %s", symbol, ", ".join(failed))
                self.logger.info("\n" + "\n".join(lines) + "\n" + "=" * 41)

        self._send_hourly_notification(now, "Trend bot is alive and checked the market.")
        self.save_state(state)

    def run_forever(self) -> None:
        symbols_str = ", ".join(self.config.symbols)
        self.logger.info(
            "Starting trend bot: symbols=%s paper=%s dry_run=%s window=%s-%s timezone=%s",
            symbols_str,
            self.config.paper,
            self.config.dry_run,
            self.config.trade_window_start,
            self.config.trade_window_end,
            self.config.timezone,
        )
        # Send startup notification via Telegram
        self.notifier.startup(self.config.symbols, self.config)
        send_macos_notification(
            f"Trend Bot Started: {symbols_str}",
            "Trend bot is now running and monitoring market conditions.",
        )
        while True:
            try:
                self.run_once()
            except Exception as exc:
                self.logger.exception("Bot cycle failed")
                self.notifier.error("poll_loop", f"{type(exc).__name__}: {exc}")
            time.sleep(max(10, int(self.config.poll_seconds)))

    def _send_hourly_notification(self, now: datetime, message: str) -> None:
        if self.last_notification_hour == now.hour:
            return
        self.last_notification_hour = now.hour
        symbols_str = ", ".join(self.config.symbols)
        title = f"Trend Bot Check-In: {symbols_str}"
        self.logger.info("Sending hourly notification: %s", message)
        send_macos_notification(title, message)


def parse_args(argv: list[str]) -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Run the trend trading bot.")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--print-config", action="store_true", help="Print resolved config and exit")
    return parser.parse_args(argv)


def _acquire_lock(lock_path: Path) -> bool:
    """Take an exclusive PID lock so two bot processes can't trade concurrently.

    Returns True if the lock was acquired, False if another live instance holds it.
    Auto-recovers a stale lock left by a crashed process.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(lock_path.read_text().strip())
            os.kill(pid, 0)  # Signal 0: raises if the process does not exist.
            return False  # A live process holds the lock.
        except (ValueError, ProcessLookupError):
            pass  # Stale or empty lock; take it over.
        except PermissionError:
            return False  # Exists but owned elsewhere -> treat as held.
    else:
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
            return True
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
            lock_path.unlink()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or None)
    config_path = Path(args.config).expanduser().resolve()
    root_dir = config_path.parent
    config = BotConfig.load(config_path)

    if args.print_config:
        print(json.dumps(config.__dict__, indent=2, sort_keys=True))
        return 0

    # Single-instance guard: refuse to run a second process that could place
    # duplicate/conflicting orders alongside the already-running instance.
    lock_path = root_dir / "state" / "bot.pid"
    if not _acquire_lock(lock_path):
        logging.getLogger(LOGGER_NAME).error(
            "Another instance appears to be running (lock: %s). Exiting to avoid duplicate trading.",
            lock_path,
        )
        return 1

    try:
        bot = TrendBot(config, root_dir)
        if args.once:
            bot.run_once()
        else:
            bot.run_forever()
    finally:
        _release_lock(lock_path)
    return 0


if __name__ == "__main__":
    import sys
    import logging

    # Set up basic logging for fatal errors
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("trend_bot")

    # Import notifier here so it's available for exception handlers
    from src.telegram_notifier import TelegramNotifier

    # Build a notifier that honors the same telegram_enabled flag as the
    # in-loop notifier, so crash / Ctrl+C alerts respect the user's setting.
    # (Credentials come from .env via load_dotenv() in alpaca_client at import.)
    def _crash_notifier() -> TelegramNotifier:
        config = BotConfig.load(parse_args(sys.argv[1:] if len(sys.argv) > 1 else None).config)
        return TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            logger=logger,
            enabled=config.telegram_enabled,
        )

    try:
        notifier = _crash_notifier()
    except Exception:
        # Never let a notifier-construction problem mask the real error.
        notifier = TelegramNotifier(None, None, logger, enabled=False)

    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        notifier.shutdown("manual stop (Ctrl+C)")
        logger.info("Bot stopped by user (Ctrl+C)")
        sys.exit(0)
    except Exception as exc:
        logger.exception("Bot crashed")
        notifier.error("fatal", f"Bot process crashed: {type(exc).__name__}: {exc}")
        raise