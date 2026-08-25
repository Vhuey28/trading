"""Regression tests for the review-fix pass.

Covers the money-path and reliability fixes that had no existing coverage:
order-outcome handling (phantom positions), unconfirmed-close handling, the
non-idempotent-POST retry guard, config validation/typo warning, atomic state
recovery, and backtest ATR sizing.
"""

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src.config import BotConfig
from src.bot import TrendBot
from src.models import TradeSignal
from src.backtest import BacktestTrade
from src.alpaca_client import AlpacaClient


class FakeClient:
    """Deterministic stand-in for AlpacaClient."""

    def __init__(self, buy_result=None, close_result=None):
        self.buy_result = buy_result or {"id": "o1", "status": "new", "avg_fill_price": "100.5", "filled_qty": "0.25"}
        self.close_result = close_result
        self.buys = []
        self.closes = []

    def place_market_buy(self, symbol, notional, client_order_id=None):
        self.buys.append((symbol, notional, client_order_id))
        return self.buy_result

    def close_position(self, symbol):
        self.closes.append(symbol)
        return self.close_result


def make_bot(tmp: Path, buy_result=None, close_result=None) -> TrendBot:
    config = BotConfig(
        symbols=["QQQ"],
        dry_run=False,
        paper=True,
        notional_usd=25.0,
        min_minutes_between_trades=0,
        max_daily_trades=999,
        state_file=str(tmp / "state.json"),
        log_dir=str(tmp / "logs"),
        data_dir=str(tmp / "data"),
        trades_csv_path=str(tmp / "trades.csv"),
        telegram_enabled=False,
        export_trades_csv=False,
    )
    bot = TrendBot(config, tmp)
    bot.client = FakeClient(buy_result=buy_result, close_result=close_result)
    return bot


class ExecuteSignalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bot = make_bot(Path(self._tmp.name))
        self.fake: FakeClient = self.bot.client  # type: ignore[assignment]

    def test_buy_accepted_records_position_with_fill_price(self):
        state: dict = {"symbols": {}}
        signal = TradeSignal(action="buy", reason="breakout entry", price=100.0)
        self.bot.execute_signal("QQQ", signal, state, {})
        ss = state["symbols"]["QQQ"]
        self.assertTrue(ss["in_position"])
        # avg_fill_price from the broker response is preferred over signal price.
        self.assertEqual(ss["entry_price"], 100.5)
        self.assertTrue(ss.get("entry_qty"))

    def test_buy_rejected_records_no_position(self):
        bot = make_bot(Path(self._tmp.name), buy_result={"id": "o1", "status": "rejected"})
        state: dict = {"symbols": {}}
        signal = TradeSignal(action="buy", reason="breakout entry", price=100.0)
        bot.execute_signal("QQQ", signal, state, {})
        # Phantom position must NOT be created for a rejected order.
        self.assertNotIn("in_position", state["symbols"])

    def test_buy_no_response_records_no_position(self):
        bot = make_bot(Path(self._tmp.name), buy_result=None)
        state: dict = {"symbols": {}}
        signal = TradeSignal(action="buy", reason="breakout entry", price=100.0)
        bot.execute_signal("QQQ", signal, state, {})
        self.assertNotIn("in_position", state["symbols"])

    def test_sell_unconfirmed_close_keeps_position(self):
        bot = make_bot(Path(self._tmp.name), close_result=None)
        state: dict = {"symbols": {"QQQ": {"in_position": True, "entry_price": 100.0}}}
        signal = TradeSignal(action="sell", reason="take profit reached", price=103.0)
        bot.execute_signal("QQQ", signal, state, {})
        ss = state["symbols"]["QQQ"]
        # An unconfirmed close must not mark us flat (prevents re-entering on top).
        self.assertTrue(ss["in_position"])

    def test_sell_confirmed_computes_pnl(self):
        bot = make_bot(Path(self._tmp.name), close_result={"id": "o2", "status": "filled", "avg_fill_price": "103.0"})
        state: dict = {"symbols": {"QQQ": {"in_position": True, "entry_price": 100.0, "entry_qty": 0.25}}}
        signal = TradeSignal(action="sell", reason="take profit reached", price=103.0)
        bot.execute_signal("QQQ", signal, state, {})
        ss = state["symbols"]["QQQ"]
        self.assertFalse(ss["in_position"])
        self.assertIsNone(ss["entry_price"])
        self.assertEqual(bot.client.closes, ["QQQ"])


class ConfigTests(unittest.TestCase):
    def test_unknown_key_warns_not_raises(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "config.json"
            cfg_file.write_text(json.dumps({"symbols": ["QQQ"], "regimes_spy_ema_50": 50}), encoding="utf-8")
            with self.assertLogs("trend_bot", level="WARNING") as cm:
                BotConfig.load(cfg_file)
            self.assertTrue(any("regimes_spy_ema_50" in out for out in cm.output))

    def test_validate_rejects_non_positive_notional(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "config.json"
            cfg_file.write_text(json.dumps({"symbols": ["QQQ"], "notional_usd": 0}), encoding="utf-8")
            with self.assertRaises(ValueError):
                BotConfig.load(cfg_file)


class RequestJsonIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config = BotConfig(state_file=str(Path(self._tmp.name) / "s.json"),
                           log_dir=str(Path(self._tmp.name) / "logs"),
                           data_dir=str(Path(self._tmp.name) / "data"))
        self.client = AlpacaClient(config, logging.getLogger("test"))
        self.client.key_id = "test-key"
        self.client.secret_key = "test-secret"

    def _urlopen_raising(self, code, calls):
        def _fake_urlopen(request, timeout):
            calls.append(request.method)
            from urllib.error import HTTPError
            raise HTTPError(request.full_url, code, f"err {code}", None, None)
        return _fake_urlopen

    def test_non_idempotent_post_is_not_retried(self):
        calls = []
        with mock.patch("src.alpaca_client.urlopen", self._urlopen_raising(500, calls)):
            with self.assertRaises(RuntimeError):
                self.client.request_json("POST", "http://example/orders", body={"x": 1})
        self.assertEqual(calls, ["POST"])  # exactly one attempt, no retry

    def test_idempotent_get_is_retried_three_times(self):
        calls = []
        with mock.patch("src.alpaca_client.urlopen", self._urlopen_raising(503, calls)):
            with self.assertRaises(RuntimeError):
                self.client.request_json("GET", "http://example/bars")
        self.assertEqual(calls.count("GET"), 3)


class BacktestTradeTests(unittest.TestCase):
    def test_close_uses_stored_atr_notional(self):
        trade = BacktestTrade(symbol="QQQ", entry_time=datetime(2026, 1, 1, 9, 31, tzinfo=timezone.utc),
                              entry_price=100.0)
        trade.notional = 50.0  # e.g. ATR sizing doubled 25 -> 50
        trade.close(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), 103.0, "target", trade.notional)
        self.assertEqual(trade.qty, 0.5)  # 50 / 100
        self.assertEqual(trade.pnl, 1.5)  # (103-100) * 0.5


class StateRecoveryTests(unittest.TestCase):
    def test_corrupt_state_recovers_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            state_file = d_path / "state.json"
            state_file.write_text("{ this is not valid json", encoding="utf-8")
            config = BotConfig(symbols=["QQQ"],
                               state_file=str(state_file),
                               log_dir=str(d_path / "logs"),
                               data_dir=str(d_path / "data"))
            bot = TrendBot(config, d_path)
            state = bot.load_state()
            self.assertEqual(state, {})
            self.assertTrue(state_file.with_suffix(state_file.suffix + ".corrupt").exists())


if __name__ == "__main__":
    unittest.main()