from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import BotConfig
from logger import configure_logging
from alpaca_client import AlpacaClient
from indicators import compute_previous_day_levels
from strategy import build_signal
from utils import (
    parse_alpaca_timestamp,
    is_trade_window_open,
    previous_data_day,
    send_macos_notification,
)
from models import Bar, PreviousDayLevels, TradeSignal


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

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
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
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def previous_day_cache_path(self, symbol: str, day: date) -> Path:
        stem = symbol_file_stem(symbol)
        return self.data_dir / f"{stem}_{day.isoformat()}_{self.config.timeframe}.csv"

    def _uses_stock_endpoint(self, symbol: str) -> bool:
        return "/" not in symbol

    def _fetch_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[Bar]:
        if self._uses_stock_endpoint(symbol):
            return self.client.fetch_stock_bars(symbol, start, end, timeframe)
        return self.client.fetch_crypto_bars(symbol, start, end, timeframe)

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
        if len(bars) >= self.config.slow_sma_period:
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

        # Return whatever we have (possibly empty — caller handles it)
        return bars

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

    def record_trade(self, state: dict[str, Any], symbol: str, signal: TradeSignal, order: dict[str, Any] | None) -> None:
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state["last_trade_at"] = now_str
        state["trade_count"] = int(state.get("trade_count", 0)) + 1
        
        symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
        symbol_state["last_trade_at"] = now_str
        symbol_state["last_signal_reason"] = signal.reason
        symbol_state["last_order_id"] = order.get("id") if order else None
        if signal.action == "buy":
            symbol_state["in_position"] = True
            symbol_state["entry_price"] = signal.price
            symbol_state["highest_price_since_entry"] = signal.price
        elif signal.action == "sell":
            symbol_state["in_position"] = False
            symbol_state["entry_price"] = None
            symbol_state["highest_price_since_entry"] = None

    def execute_signal(self, symbol: str, signal: TradeSignal, state: dict[str, Any]) -> None:
        if signal.action == "buy" and not self.can_trade_now(state, symbol, datetime.now(self.tz)):
            self.logger.info("[%s] Signal skipped because trade limits are active: %s", symbol, signal.reason)
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
            return

        if signal.action == "buy":
            order = self.client.place_market_buy(symbol, self.config.notional_usd)
        elif signal.action == "sell":
            order = self.client.close_position(symbol)
        else:
            order = None
        self.logger.info("[%s] Placed %s order: %s", symbol, signal.action, order)
        self.record_trade(state, symbol, signal, order)

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

        for symbol in self.config.symbols:
            symbol_state = state.setdefault("symbols", {}).setdefault(symbol, {})
            self.sync_live_position(symbol, symbol_state)

            try:
                levels = self.get_previous_day_levels(symbol, now)
            except Exception as e:
                self.logger.error("[%s] Failed to get previous-day levels: %s", symbol, e)
                continue

            recent_bars = self.fetch_recent_bars(symbol, now)
            if not recent_bars:
                self.logger.info("[%s] No recent bars fetched", symbol)
                continue

            # compute indicators for logging
            ordered = sorted(recent_bars, key=lambda bar: bar.timestamp)
            closes = [bar.close for bar in ordered]
            price = closes[-1] if closes else None
            from indicators import sma, atr, rsi, avg_volume

            fast = sma(closes, self.config.fast_sma_period)
            slow = sma(closes, self.config.slow_sma_period)
            atr_val = atr(ordered)
            rsi_val = rsi(closes)
            avg_vol = avg_volume(ordered)
            vol_ratio = None
            last_vol = ordered[-1].volume if ordered else None
            if avg_vol and last_vol is not None and avg_vol > 0:
                vol_ratio = last_vol / avg_vol

            breakout_price = levels.high * (1.0 + self.config.breakout_buffer_pct)

            checks = {
                "Trend Up": levels.trend == "up",
                "Fast SMA > Slow SMA": (fast is not None and slow is not None and fast > slow),
                "Breakout": (price is not None and price >= breakout_price),
                "Volume": (vol_ratio is not None and vol_ratio > 1.5),
                "RSI": (rsi_val is not None and 40 <= rsi_val <= 70),
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
            lines.append(f"Breakout         {breakout_price:.2f}")
            lines.append("")
            for name, ok in checks.items():
                lines.append(("✓ " if ok else "✗ ") + name)
            lines.append("")

            signal = build_signal(levels, recent_bars, symbol_state, self.config)
            if signal:
                lines.append(f"{signal.action.title()} - {signal.reason}")
                self.logger.info("\n" + "\n".join(lines) + "\n" + "=" * 41)
                self.execute_signal(symbol, signal, state)
            else:
                lines.append("No Trade")
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
        send_macos_notification(
            f"Trend Bot Started: {symbols_str}",
            "Trend bot is now running and monitoring market conditions.",
        )
        while True:
            try:
                self.run_once()
            except Exception:
                self.logger.exception("Bot cycle failed")
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or None)
    config_path = Path(args.config).expanduser().resolve()
    root_dir = config_path.parent
    config = BotConfig.load(config_path)

    if args.print_config:
        print(json.dumps(config.__dict__, indent=2, sort_keys=True))
        return 0

    bot = TrendBot(config, root_dir)
    if args.once:
        bot.run_once()
    else:
        bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
