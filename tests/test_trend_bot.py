import unittest
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_bot import (  # noqa: E402
    AlpacaClient,
    Bar,
    BotConfig,
    build_signal,
    compute_previous_day_levels,
    is_trade_window_open,
)


class CapturingClient(AlpacaClient):
    def __init__(self, config: BotConfig) -> None:
        super().__init__(config, logging.getLogger("trend_bot_test"))
        self.requests = []

    def request_json(
        self,
        method,
        url,
        params=None,
        body=None,
        expected_404=False,
    ):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "body": body,
                "expected_404": expected_404,
            }
        )
        if method == "GET":
            return {
                "bars": {
                    "QQQ": [
                        {
                            "t": "2026-07-02T14:30:00Z",
                            "o": 500,
                            "h": 501,
                            "l": 499,
                            "c": 500.5,
                            "v": 1000,
                        }
                    ]
                }
            }
        return {"id": "order-1"}


class TradingWindowTests(unittest.TestCase):
    def test_window_crosses_midnight(self) -> None:
        tz = ZoneInfo("America/Los_Angeles")
        self.assertTrue(is_trade_window_open(datetime(2026, 7, 2, 6, 0, tzinfo=tz), "06:00", "01:00"))
        self.assertTrue(is_trade_window_open(datetime(2026, 7, 2, 23, 59, tzinfo=tz), "06:00", "01:00"))
        self.assertTrue(is_trade_window_open(datetime(2026, 7, 3, 0, 59, tzinfo=tz), "06:00", "01:00"))
        self.assertFalse(is_trade_window_open(datetime(2026, 7, 3, 1, 0, tzinfo=tz), "06:00", "01:00"))
        self.assertFalse(is_trade_window_open(datetime(2026, 7, 3, 5, 59, tzinfo=tz), "06:00", "01:00"))


class StrategyTests(unittest.TestCase):
    def make_bars(self, closes: list[float]) -> list[Bar]:
        base = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
        bars = []
        for index, close in enumerate(closes):
            bars.append(
                Bar(
                    timestamp=base + timedelta(minutes=index),
                    open=close - 0.5,
                    high=close + 0.5,
                    low=close - 1.0,
                    close=close,
                    volume=100,
                )
            )
        return bars

    def test_previous_day_levels_detect_uptrend(self) -> None:
        bars = self.make_bars([100, 101, 102, 103])
        levels = compute_previous_day_levels(date(2026, 7, 1), bars)
        self.assertEqual(levels.trend, "up")
        self.assertEqual(levels.high, 103.5)
        self.assertEqual(levels.low, 99.0)

    def test_buy_signal_requires_breakout_and_ma_alignment(self) -> None:
        config = BotConfig(
            fast_sma_period=3,
            slow_sma_period=5,
            breakout_buffer_pct=0.0,
            use_breakout_entry=True,
            use_atr_breakout=False,
            use_pullback_entry=False,
            use_vwap_continuation_entry=False,
            use_multi_timeframe=False,
            require_previous_day_uptrend=True,
            use_adaptive_volume=False,
            minimum_volume_multiplier=0.0,
            volume_period=3,
            atr_period=3,
            rsi_period=3,
            # Disable incidental filters so this test isolates breakout + MA
            # alignment. The fixture bars are stamped 12:00 UTC, which the
            # time-of-day filters would otherwise (wrongly) treat as ET lunch.
            use_lunch_filter=False,
            use_market_open_delay=False,
            use_low_vol_filter=False,
            use_adx_filter=False,
            use_market_regime_filter=False,
        )
        previous = compute_previous_day_levels(date(2026, 7, 1), self.make_bars([100, 101, 102, 103, 104]))
        recent = self.make_bars([103, 104, 105, 106, 107])
        signal = build_signal(previous, recent, {}, config)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "buy")

    def test_sell_signal_on_stop_loss(self) -> None:
        config = BotConfig(
            fast_sma_period=3,
            slow_sma_period=5,
            stop_loss_pct=0.02,
            use_atr_stops=False,
        )
        previous = compute_previous_day_levels(date(2026, 7, 1), self.make_bars([100, 101, 102, 103, 104]))
        state = {"in_position": True, "entry_price": 100.0, "highest_price_since_entry": 105.0}
        recent = self.make_bars([102, 101, 100, 99, 97])
        signal = build_signal(previous, recent, state, config)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "sell")
        self.assertIn("stop loss", signal.reason)


class StockAlpacaClientTests(unittest.TestCase):
    def test_fetch_stock_bars_uses_stock_endpoint_and_feed(self) -> None:
        config = BotConfig(symbol="QQQ", stock_feed="iex", stock_adjustment="split")
        client = CapturingClient(config)
        bars = client.fetch_stock_bars(
            "QQQ",
            datetime(2026, 7, 2, 13, 30, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 7, 2, 14, 30, tzinfo=ZoneInfo("UTC")),
            "1Min",
        )

        self.assertEqual(len(bars), 1)
        request = client.requests[-1]
        self.assertEqual(request["method"], "GET")
        self.assertTrue(request["url"].endswith("/v2/stocks/bars"))
        self.assertEqual(request["params"]["symbols"], "QQQ")
        self.assertEqual(request["params"]["feed"], "iex")
        self.assertEqual(request["params"]["adjustment"], "split")

    def test_place_limit_buy_uses_extended_hours_stock_order(self) -> None:
        config = BotConfig(symbol="QQQ", limit_price_offset_pct=0.002, extended_hours=True)
        client = CapturingClient(config)
        client.place_limit_buy("QQQ", notional_usd=25.0, reference_price=500.0)

        body = client.requests[-1]["body"]
        self.assertEqual(body["symbol"], "QQQ")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["type"], "limit")
        self.assertEqual(body["time_in_force"], "day")
        self.assertEqual(body["qty"], "0.05")
        self.assertEqual(body["limit_price"], "501.00")
        self.assertTrue(body["extended_hours"])


class PreviousDataDayTests(unittest.TestCase):
    def test_previous_data_day_skips_weekends(self) -> None:
        from utils import previous_data_day
        # Monday -> Friday
        self.assertEqual(previous_data_day(datetime(2026, 7, 13, 10, 0)), date(2026, 7, 10))
        # Sunday -> Friday
        self.assertEqual(previous_data_day(datetime(2026, 7, 12, 10, 0)), date(2026, 7, 10))
        # Saturday -> Friday
        self.assertEqual(previous_data_day(datetime(2026, 7, 11, 10, 0)), date(2026, 7, 10))
        # Friday -> Thursday
        self.assertEqual(previous_data_day(datetime(2026, 7, 10, 10, 0)), date(2026, 7, 9))
        # Tuesday -> Monday
        self.assertEqual(previous_data_day(datetime(2026, 7, 14, 10, 0)), date(2026, 7, 13))


class TrendBotTests(unittest.TestCase):
    def test_state_migration(self) -> None:
        from bot import TrendBot
        from config import BotConfig
        import tempfile
        import shutil
        from pathlib import Path

        # Create a temp dir for testing state file
        temp_dir = tempfile.mkdtemp()
        try:
            state_file = Path(temp_dir) / "state.json"
            # Write old state format
            old_state = {
                "in_position": True,
                "entry_price": 500.0,
                "highest_price_since_entry": 510.0,
                "last_trade_at": "2026-07-02T14:30:00Z",
                "last_signal_reason": "breakout",
                "last_order_id": "order-1",
                "trade_count_day": "2026-07-02",
                "trade_count": 1,
            }
            import json
            state_file.write_text(json.dumps(old_state), encoding="utf-8")

            config = BotConfig(symbol="QQQ", state_file=str(state_file))
            bot = TrendBot(config, Path(temp_dir))
            
            # Load state and verify it migrated
            migrated = bot.load_state()
            self.assertNotIn("in_position", migrated)
            self.assertIn("symbols", migrated)
            self.assertIn("QQQ", migrated["symbols"])
            self.assertTrue(migrated["symbols"]["QQQ"]["in_position"])
            self.assertEqual(migrated["symbols"]["QQQ"]["entry_price"], 500.0)
            self.assertEqual(migrated["symbols"]["QQQ"]["highest_price_since_entry"], 510.0)
            self.assertEqual(migrated["trade_count"], 1)
        finally:
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    unittest.main()
