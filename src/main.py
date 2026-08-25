from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from datetime import datetime
from typing import List

from config import BotConfig
from bot import TrendBot


def format_currency(value: float) -> str:
    return f"${value:,.2f}"


def _make_bot_for_symbol(config: BotConfig, root: Path, symbol: str) -> TrendBot:
    cfg_kwargs = {k: getattr(config, k) for k in config.__dataclass_fields__.keys()}
    cfg_kwargs["symbol"] = symbol
    # CRITICAL: each per-symbol bot must trade ONLY its own symbol. Without this,
    # every thread would iterate the full `symbols` list and place Nx duplicate /
    # conflicting orders across threads. Prefer the single-bot path (python -m
    # src.bot) for production; see README.
    cfg_kwargs["symbols"] = [symbol]
    cfg_kwargs["state_file"] = f"state/{symbol}_state.json"
    sym_cfg = BotConfig(**cfg_kwargs)
    return TrendBot(sym_cfg, root)


def run_once_all(config_path: str | Path) -> None:
    config_path = Path(config_path)
    root = config_path.parent
    config = BotConfig.load(config_path)

    threads: List[threading.Thread] = []
    bots: List[TrendBot] = []
    for symbol in config.symbols:
        bot = _make_bot_for_symbol(config, root, symbol)
        bots.append(bot)
        thread = threading.Thread(target=bot.run_once, name=f"run_once-{symbol}")
        thread.start()
        threads.append(thread)

    for t in threads:
        t.join()

    _print_daily_report(config, root)


def run_forever_all(config_path: str | Path) -> None:
    config_path = Path(config_path)
    root = config_path.parent
    config = BotConfig.load(config_path)

    threads: List[threading.Thread] = []
    for symbol in config.symbols:
        bot = _make_bot_for_symbol(config, root, symbol)
        thread = threading.Thread(target=bot.run_forever, name=f"bot-{symbol}", daemon=True)
        thread.start()
        threads.append(thread)

    # keep main thread alive
    try:
        while True:
            for t in threads:
                if not t.is_alive():
                    # if any thread exited, log and continue
                    pass
            threading.Event().wait(60)
    except KeyboardInterrupt:
        return


def _print_daily_report(config: BotConfig, root: Path) -> None:
    report_lines = []
    report_lines.append("=" * 30)
    report_lines.append("")
    report_lines.append("Daily Summary")
    report_lines.append("")
    total_open = 0
    total_cash = 0.0
    total_today_return = 0.0
    count_returns = 0

    for symbol in config.symbols:
        state_path = root / f"state/{symbol}_state.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        trades = int(state.get("trade_count", 0))
        win_rate = state.get("win_rate")
        realized = float(state.get("realized_pnl", 0.0))
        today_return = float(state.get("today_return", 0.0))
        cash = float(state.get("cash", 0.0)) if state.get("cash") is not None else None
        open_pos = 1 if state.get("in_position") else 0

        report_lines.append("")
        report_lines.append(symbol)
        report_lines.append("")
        report_lines.append(f"Trades: {trades}")
        report_lines.append("")
        report_lines.append(f"Win Rate: {int(win_rate * 100)}%" if win_rate is not None else "Win Rate: -")
        report_lines.append("")
        report_lines.append("Profit:")
        report_lines.append(f"{format_currency(realized) if realized >= 0 else '-' + format_currency(-realized)}")
        report_lines.append("")
        report_lines.append("-" * 30)

        total_open += open_pos
        if cash is not None:
            total_cash += cash
        total_today_return += today_return
        count_returns += 1

    report_lines.append("")
    report_lines.append("Portfolio")
    report_lines.append("")
    avg_return = (total_today_return / count_returns) if count_returns else 0.0
    report_lines.append("Today's Return:")
    report_lines.append(f"{avg_return:+.2f}%")
    report_lines.append("")
    report_lines.append(f"Open Positions:")
    report_lines.append(str(total_open))
    report_lines.append("")
    report_lines.append("Cash:")
    report_lines.append(format_currency(total_cash))
    report_lines.append("")
    report_lines.append("=" * 30)

    print("\n".join(report_lines))


def main(argv: list[str] | None = None) -> int:
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.once:
        run_once_all(args.config)
    else:
        run_forever_all(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
