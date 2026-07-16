"""
backtest.py — Trend-bot backtester
====================================
Replays 1-minute bar data through the exact same strategy used by the live
bot and produces a P&L summary report per symbol.

Usage (run from the repo root):
    python src/backtest.py --start 2026-07-01 --end 2026-07-10
    python src/backtest.py --start 2026-07-01 --end 2026-07-10 --symbols QQQ SPY
    python src/backtest.py --start 2026-07-01 --end 2026-07-10 --fetch

The script uses the same config.json and .env as the live bot.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap sys.path so local modules resolve (same trick as trend_bot.py)
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import BotConfig
from indicators import compute_previous_day_levels
from models import Bar, PreviousDayLevels, TradeSignal
from strategy import build_signal
from utils import parse_alpaca_timestamp, previous_data_day


# ── helpers ──────────────────────────────────────────────────────────────────

def symbol_file_stem(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _weekdays_in_range(start: date, end: date) -> list[date]:
    # Return every Mon–Fri between start (inclusive) and end (inclusive).
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:   # 0=Mon … 4=Fri
            days.append(cur)
        cur += timedelta(days=1)
    return days


def read_bars_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return bars
    reader = csv.DictReader(lines, delimiter=",")
    for row in reader:
        try:
            bars.append(Bar(
                timestamp=parse_alpaca_timestamp(
                    row.get("timestamp") or row.get("time") or next(iter(row.values()))
                ),
                open=float(row.get("open") or row.get("o") or 0.0),
                high=float(row.get("high") or row.get("h") or 0.0),
                low=float(row.get("low") or row.get("l") or 0.0),
                close=float(row.get("close") or row.get("c") or 0.0),
                volume=float(row.get("volume") or row.get("v") or 0.0),
            ))
        except Exception:
            continue
    return bars


def write_bars_csv(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["timestamp", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        for bar in sorted(bars, key=lambda b: b.timestamp):
            writer.writerow({
                "timestamp": bar.timestamp.astimezone(timezone.utc)
                             .isoformat().replace("+00:00", "Z"),
                "open": bar.open, "high": bar.high,
                "low": bar.low,  "close": bar.close, "volume": bar.volume,
            })


def fetch_and_cache(
    symbol: str, day: date, config: BotConfig,
    data_dir: Path, logger: logging.Logger,
) -> list[Bar]:
    # Download bars for *symbol* on *day* from Alpaca and cache to CSV.
    from alpaca_client import AlpacaClient
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.timezone)
    start_dt = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_dt   = start_dt + timedelta(days=1)
    client   = AlpacaClient(config, logger)

    if "/" in symbol:
        bars = client.fetch_crypto_bars(symbol, start_dt, end_dt, config.timeframe)
    else:
        bars = client.fetch_stock_bars(symbol, start_dt, end_dt, config.timeframe)

    if bars:
        path = data_dir / f"{symbol_file_stem(symbol)}_{day.isoformat()}_{config.timeframe}.csv"
        write_bars_csv(path, bars)
        logger.info("[%s] Fetched and cached %d bars for %s", symbol, len(bars), day)
    else:
        logger.warning("[%s] No bars from Alpaca for %s", symbol, day)
    return bars


def _load_day(
    symbol: str, day: date, config: BotConfig,
    data_dir: Path, logger: logging.Logger, fetch_missing: bool,
) -> list[Bar] | None:
    # Load (optionally fetch) bars for one symbol/day. Returns None on miss.
    path = data_dir / f"{symbol_file_stem(symbol)}_{day.isoformat()}_{config.timeframe}.csv"
    if not path.exists():
        if fetch_missing:
            fetch_and_cache(symbol, day, config, data_dir, logger)
        if not path.exists():
            return None
    return read_bars_csv(path) or None


# ── trade / result tracking ───────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol:      str
    entry_time:  datetime
    entry_price: float
    exit_time:   datetime | None = None
    exit_price:  float    | None = None
    exit_reason: str  = ""
    pnl:         float = 0.0
    pnl_pct:     float = 0.0

    def close(self, exit_time: datetime, exit_price: float, reason: str, notional: float) -> None:
        self.exit_time   = exit_time
        self.exit_price  = exit_price
        self.exit_reason = reason
        qty           = notional / self.entry_price
        self.pnl      = (exit_price - self.entry_price) * qty
        self.pnl_pct  = (exit_price / self.entry_price - 1.0) * 100.0


@dataclass
class SymbolResult:
    symbol:       str
    trades:       list[BacktestTrade] = field(default_factory=list)
    days_tested:  int   = 0
    total_pnl:    float = 0.0
    wins:         int   = 0
    losses:       int   = 0

    def finish(self) -> None:
        closed = [t for t in self.trades if t.exit_price is not None]
        self.total_pnl = sum(t.pnl for t in closed)
        self.wins      = sum(1 for t in closed if t.pnl > 0)
        self.losses    = sum(1 for t in closed if t.pnl <= 0)


@dataclass
class BacktestSummary:
    """Aggregated summary across all symbols"""
    total_trades: int = 0
    total_pnl: float = 0.0
    total_wins: int = 0
    total_losses: int = 0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_wins / self.total_trades * 100.0

    @property
    def avg_trade(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades


def calculate_equity_curve(trades: list[BacktestTrade]) -> list[float]:
    """Calculate equity curve from trades"""
    equity = 0.0
    curve = []
    for trade in trades:
        if trade.pnl is not None:
            equity += trade.pnl
            curve.append(equity)
    return curve


def calculate_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Calculate max drawdown and max drawdown %"""
    if not equity_curve:
        return 0.0, 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        dd = peak - value
        if dd > max_dd:
            max_dd = dd
        if peak != 0:
            dd_pct = dd / peak * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
    return max_dd, max_dd_pct


def calculate_profit_factor(trades: list[BacktestTrade]) -> float:
    """Calculate profit factor (gross profit / gross loss)"""
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    if gross_loss == 0:
        return float('inf') if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def calculate_expectancy(trades: list[BacktestTrade]) -> float:
    """Calculate expectancy (average P&L per trade)"""
    if not trades:
        return 0.0
    return sum(t.pnl for t in trades) / len(trades)


def calculate_consecutive(trades: list[BacktestTrade]) -> tuple[int, int]:
    """Calculate max consecutive wins and losses"""
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for trade in trades:
        if trade.pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    return max_wins, max_losses


def compute_summary(results: list[SymbolResult]) -> BacktestSummary:
    """Compute aggregate summary statistics across all symbols"""
    all_trades = []
    for r in results:
        closed = [t for t in r.trades if t.exit_price is not None]
        all_trades.extend(closed)

    all_trades.sort(key=lambda t: t.entry_time)

    summary = BacktestSummary()
    summary.total_trades = len(all_trades)
    summary.total_wins = sum(1 for t in all_trades if t.pnl > 0)
    summary.total_losses = sum(1 for t in all_trades if t.pnl <= 0)
    summary.total_pnl = sum(t.pnl for t in all_trades)

    if all_trades:
        equity_curve = calculate_equity_curve(all_trades)
        summary.max_drawdown, _ = calculate_drawdown(equity_curve)
        summary.profit_factor = calculate_profit_factor(all_trades)
        summary.expectancy = calculate_expectancy(all_trades)
        summary.avg_win = sum(t.pnl for t in all_trades if t.pnl > 0) / max(1, summary.total_wins)
        summary.avg_loss = sum(t.pnl for t in all_trades if t.pnl <= 0) / max(1, summary.total_losses)
        summary.largest_win = max(t.pnl for t in all_trades)
        summary.largest_loss = min(t.pnl for t in all_trades)
        summary.max_consecutive_wins, summary.max_consecutive_losses = calculate_consecutive(all_trades)

    return summary


# ── per-symbol backtester ─────────────────────────────────────────────────────

def backtest_symbol(
    symbol: str,
    days: list[date],
    config: BotConfig,
    data_dir: Path,
    logger: logging.Logger,
    fetch_missing: bool = False,
) -> SymbolResult:
    result = SymbolResult(symbol=symbol)

    # Per-symbol mutable state (mirrors the live bot's per-symbol state dict)
    sym_state: dict[str, Any] = {
        "in_position": False,
        "entry_price": None,
        "highest_price_since_entry": None,
    }
    global_state: dict[str, Any] = {
        "trade_count": 0,
        "trade_count_day": None,
    }

    open_trade: BacktestTrade | None = None

    for day in days:
        # ── previous-day levels ──────────────────────────────────────────
        prev_day = previous_data_day(
            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        )
        prev_bars = _load_day(symbol, prev_day, config, data_dir, logger, fetch_missing)
        if not prev_bars:
            logger.warning("[%s] No prev-day data for %s → skipping %s", symbol, prev_day, day)
            continue
        try:
            levels = compute_previous_day_levels(prev_day, prev_bars)
        except Exception as exc:
            logger.warning("[%s] Level compute failed for %s: %s", symbol, prev_day, exc)
            continue

        # ── current-day bars ─────────────────────────────────────────────
        cur_bars = _load_day(symbol, day, config, data_dir, logger, fetch_missing)
        if not cur_bars:
            logger.warning("[%s] No data for %s — skipping", symbol, day)
            continue

        cur_bars.sort(key=lambda b: b.timestamp)
        result.days_tested += 1

        # Reset daily trade counter
        day_key = day.isoformat()
        if global_state["trade_count_day"] != day_key:
            global_state["trade_count_day"] = day_key
            global_state["trade_count"] = 0

        # ── replay bars minute-by-minute ─────────────────────────────────
        for i in range(config.slow_sma_period, len(cur_bars) + 1):
            window      = cur_bars[:i]
            current_bar = cur_bars[i - 1]

            if global_state["trade_count"] >= config.max_daily_trades:
                break

            signal = build_signal(levels, window, sym_state, config)
            if signal is None:
                continue

            ts = current_bar.timestamp

            if signal.action == "buy" and not sym_state["in_position"]:
                open_trade = BacktestTrade(
                    symbol=symbol,
                    entry_time=ts,
                    entry_price=signal.price,
                )
                sym_state["in_position"]             = True
                sym_state["entry_price"]             = signal.price
                sym_state["highest_price_since_entry"] = signal.price
                global_state["trade_count"]         += 1
                logger.debug("[%s] BUY  @ %8.4f  %s  (%s)",
                             symbol, signal.price, ts.strftime("%Y-%m-%d %H:%M"), signal.reason)

            elif signal.action == "sell" and sym_state["in_position"] and open_trade:
                open_trade.close(ts, signal.price, signal.reason, config.notional_usd)
                result.trades.append(open_trade)
                open_trade = None
                sym_state["in_position"]              = False
                sym_state["entry_price"]              = None
                sym_state["highest_price_since_entry"] = None
                last_t = result.trades[-1]
                logger.debug("[%s] SELL @ %8.4f  %s  (%s)  pnl=$%.2f",
                             symbol, last_t.exit_price, ts.strftime("%Y-%m-%d %H:%M"),
                             last_t.exit_reason, last_t.pnl)

        # Close any position left open at end of day
        if sym_state["in_position"] and open_trade and config.exit_when_window_closes:
            last_bar = cur_bars[-1]
            open_trade.close(
                last_bar.timestamp, last_bar.close,
                "end-of-day close", config.notional_usd,
            )
            result.trades.append(open_trade)
            open_trade = None
            sym_state["in_position"]              = False
            sym_state["entry_price"]              = None
            sym_state["highest_price_since_entry"] = None

    result.finish()
    return result


# ── CSV export ────────────────────────────────────────────────────────────────

def export_trades_csv(results: list[SymbolResult], config: BotConfig, root_dir: Path) -> Path:
    """Export all trades to CSV file"""
    csv_path = root_dir / config.trades_csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    all_trades = []
    for r in results:
        for t in r.trades:
            if t.exit_price is not None:
                all_trades.append(t)

    all_trades.sort(key=lambda t: t.entry_time)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "entry_time", "exit_time", "entry_price", "exit_price",
            "pnl", "pnl_pct", "exit_reason", "notional_usd"
        ])
        for t in all_trades:
            writer.writerow([
                t.symbol,
                t.entry_time.isoformat(),
                t.exit_time.isoformat() if t.exit_time else "",
                t.entry_price,
                t.exit_price,
                t.pnl,
                t.pnl_pct,
                t.exit_reason,
                config.notional_usd,
            ])

    return csv_path


def export_equity_curve_csv(results: list[SymbolResult], config: BotConfig, root_dir: Path) -> Path:
    """Export equity curve to CSV"""
    csv_path = root_dir / config.log_dir / "equity_curve.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    all_trades = []
    for r in results:
        for t in r.trades:
            if t.exit_price is not None:
                all_trades.append(t)

    all_trades.sort(key=lambda t: t.entry_time)

    equity = 0.0
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "equity", "trade_pnl", "symbol"])
        for t in all_trades:
            equity += t.pnl
            writer.writerow([
                t.exit_time.isoformat(),
                equity,
                t.pnl,
                t.symbol,
            ])

    return csv_path


# ── report printer ────────────────────────────────────────────────────────────

def print_report(results: list[SymbolResult], config: BotConfig) -> None:
    W = 70
    print(f"\n{'='*W}")
    print("  BACKTEST RESULTS")
    print(f"  notional=${config.notional_usd:.2f}  "
          f"stop={config.stop_loss_pct*100:.1f}%  "
          f"tp={config.take_profit_pct*100:.1f}%  "
          f"trail={config.trail_stop_pct*100:.1f}%")
    if config.use_atr_stops:
        print(f"  ATR stops: SL={config.atr_stop_loss_multiplier}x TP={config.atr_take_profit_multiplier}x Trail={config.atr_trail_stop_multiplier}x")
    print(f"{'='*W}")

    for r in results:
        closed = [t for t in r.trades if t.exit_price is not None]
        if not closed:
            print(f"\n  {r.symbol:6s} — no completed trades over {r.days_tested} day(s)")
            continue

        total_trades = len(closed)
        win_rate     = r.wins / total_trades * 100 if total_trades else 0.0
        avg_pnl      = r.total_pnl / total_trades if total_trades else 0.0
        best         = max(t.pnl for t in closed)
        worst        = min(t.pnl for t in closed)

        # Per-symbol stats
        equity_curve = calculate_equity_curve(closed)
        max_dd, max_dd_pct = calculate_drawdown(equity_curve)
        profit_factor = calculate_profit_factor(closed)
        expectancy = calculate_expectancy(closed)
        max_consec_wins, max_consec_losses = calculate_consecutive(closed)

        avg_win = sum(t.pnl for t in closed if t.pnl > 0) / max(1, r.wins)
        avg_loss = sum(t.pnl for t in closed if t.pnl <= 0) / max(1, r.losses)

        print(f"\n  {r.symbol} ({r.days_tested} trading days)")
        print(f"    Trades       : {total_trades}  ({r.wins}W / {r.losses}L,  {win_rate:.0f}% win rate)")
        print(f"    Net P&L      : ${r.total_pnl:+.2f}")
        print(f"    Avg/trade    : ${avg_pnl:+.2f}")
        print(f"    Best trade   : ${best:+.2f}")
        print(f"    Worst trade  : ${worst:+.2f}")
        print(f"    Avg Win      : ${avg_win:+.2f}")
        print(f"    Avg Loss     : ${avg_loss:+.2f}")
        print(f"    Profit Factor: {profit_factor:.2f}" if profit_factor != float('inf') else "    Profit Factor: ∞")
        print(f"    Expectancy   : ${expectancy:+.2f}")
        print(f"    Max Drawdown : -${max_dd:.2f} ({max_dd_pct:.1f}%)")
        print(f"    Max Consec W : {max_consec_wins}")
        print(f"    Max Consec L : {max_consec_losses}")

        # Trade details
        print()
        hdr = f"    {'Entry':16}  {'Exit':16}  {'Entry$':>8}  {'Exit$':>8}  {'P&L':>8}  Reason"
        print(hdr)
        print(f"    {'-'*16}  {'-'*16}  {'-'*8}  {'-'*8}  {'-'*8}  ------")
        for t in closed:
            entry_s = t.entry_time.strftime("%m-%d %H:%M")
            exit_s  = t.exit_time.strftime("%m-%d %H:%M") if t.exit_time else "open  "
            flag    = "✓" if t.pnl > 0 else "✗"
            print(f"    {entry_s:16}  {exit_s:16}  "
                  f"${t.entry_price:>7.2f}  ${t.exit_price:>7.2f}  "
                  f"${t.pnl:>+7.2f}  {flag} {t.exit_reason}")

    # Grand summary
    summary = compute_summary(results)
    print(f"\n{'='*W}")
    print(f"  ALL SYMBOLS  {summary.total_trades} trades  "
          f"{summary.total_wins}W / {summary.total_losses}L  "
          f"{summary.win_rate:.0f}% win rate  Net P&L ${summary.total_pnl:+.2f}")
    print(f"  Avg Trade    : ${summary.avg_trade:+.2f}")
    print(f"  Profit Factor: {summary.profit_factor:.2f}" if summary.profit_factor != float('inf') else "  Profit Factor: ∞")
    print(f"  Expectancy   : ${summary.expectancy:+.2f}")
    print(f"  Max Drawdown : ${summary.max_drawdown:+.2f}")
    print(f"  Max Consec W : {summary.max_consecutive_wins}")
    print(f"  Max Consec L : {summary.max_consecutive_losses}")
    print(f"  Largest Win  : ${summary.largest_win:+.2f}")
    print(f"  Largest Loss : ${summary.largest_loss:+.2f}")
    print(f"{'='*W}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest the trend strategy over historical bar data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config",   default="config.json",
                   help="Path to config.json (default: config.json)")
    p.add_argument("--start",    required=True,
                   help="Start date inclusive, YYYY-MM-DD")
    p.add_argument("--end",      required=True,
                   help="End date inclusive, YYYY-MM-DD")
    p.add_argument("--symbols",  nargs="+", default=None,
                   help="Override symbols (default: use config.json)")
    p.add_argument("--fetch",    action="store_true",
                   help="Download missing bar CSVs from Alpaca before testing")
    p.add_argument("--export-csv", action="store_true",
                   help="Export trades and equity curve to CSV")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print every buy/sell signal (DEBUG logging)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logger = logging.getLogger("backtest")

    # Load .env if present
    root_dir = Path(args.config).expanduser().resolve().parent
    env_file = root_dir / ".env"
    if env_file.exists():
        for raw_line in env_file.read_text().splitlines():
            raw_line = raw_line.strip()
            if raw_line.startswith("export "):
                raw_line = raw_line[7:]
            if "=" in raw_line and not raw_line.startswith("#"):
                k, _, v = raw_line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    config   = BotConfig.load(Path(args.config).expanduser().resolve())
    data_dir = root_dir / config.data_dir
    symbols  = args.symbols or config.symbols

    try:
        start_date = date.fromisoformat(args.start)
        end_date   = date.fromisoformat(args.end)
    except ValueError as exc:
        logger.error("Bad date: %s", exc)
        return 1

    if start_date > end_date:
        logger.error("--start must be on or before --end")
        return 1

    days = _weekdays_in_range(start_date, end_date)
    if not days:
        logger.error("No weekdays in range %s – %s", start_date, end_date)
        return 1

    logger.info("Symbols : %s", ", ".join(symbols))
    logger.info("Range   : %s → %s  (%d trading day(s))", start_date, end_date, len(days))
    if args.fetch:
        logger.info("--fetch enabled: will download missing days from Alpaca")

    results: list[SymbolResult] = []
    for symbol in symbols:
        logger.info("── %s ─────────────────────────────────────────", symbol)
        r = backtest_symbol(
            symbol=symbol, days=days, config=config,
            data_dir=data_dir, logger=logger,
            fetch_missing=args.fetch,
        )
        results.append(r)

    print_report(results, config)

    # Export CSVs if requested
    if args.export_csv or config.export_trades_csv:
        trades_path = export_trades_csv(results, config, root_dir)
        equity_path = export_equity_curve_csv(results, config, root_dir)
        print(f"\n  Exported trades to: {trades_path}")
        print(f"  Exported equity curve to: {equity_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())