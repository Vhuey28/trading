# Trend Trading Bot

This folder contains a small Alpaca-based stock and ETF trend trading bot that is designed to run on your Mac and only trade during your requested local window: **6:00 AM through 1:00 AM**.

The bot is safe by default:

- It uses Alpaca paper trading endpoints by default.
- It starts in `dry_run` mode, so it logs the trades it would make before placing real paper or live orders.
- It stores no API keys in the code.

## What It Trades

The default symbol is `QQQ`. You can change the stock or ETF symbol and sizing in `config.json`.

Common symbols you can try in paper trading:

- `QQQ`
- `SPY`
- `IWM`
- `DIA`
- `AAPL`
- `MSFT`
- `NVDA`
- `AMD`
- `TSLA`

The bot uses Alpaca's stock bars endpoint with the `iex` feed by default, which is usually the right starting point for paper accounts. If your Alpaca account has a different market data subscription, you can change `stock_feed` in `config.json`.

## Strategy

Each day, the bot reads the previous day's 1-minute bars and computes:

- Previous-day high and low
- Previous-day open and close
- Previous-day VWAP
- Previous-day trend direction

During the trading window, it looks for a long trend setup:

- Previous day closed above its open and above VWAP
- Current short moving average is above the longer moving average
- Current price breaks above the previous day's high by the configured buffer

It exits on a stop loss, take profit, trailing stop, moving-average trend break, or when the trading window closes if `exit_when_window_closes` is enabled.

For stocks and ETFs, the bot sends limit orders with `extended_hours` enabled. That is more compatible with your 6:00 AM to 1:00 AM window than regular market-only orders, but the asset still has to be eligible and liquid enough for the order to fill.

## Setup

1. Open `config.json` and review the symbol, trade size, and risk settings.
2. Create a `.env` file with your Alpaca credentials:

```sh
export ALPACA_API_KEY="your_key_here"
export ALPACA_SECRET_KEY="your_secret_here"
```

3. Leave `"dry_run": true` until you have watched the bot run and reviewed the logs.

## Run Manually

Double-click:

```text
run_trend_bot.command
```

Logs are written to:

```text
logs/trend_bot.log
```

## Check Alpaca Credentials

If you see `401 Unauthorized`, Alpaca rejected the key and secret being sent to the API. Run:

```sh
cd /Users/vaughnhuey/Documents/trading
set -a; source .env; set +a
python3 src/trend_bot.py --config config.json --check-credentials
```

If that fails, regenerate your Alpaca paper trading API key and secret, then update `.env`.

## Run Automatically

Double-click:

```text
install_auto_start.command
```

That installs a macOS LaunchAgent. The LaunchAgent keeps the bot running, and the bot itself enforces the 6:00 AM to 1:00 AM trading window.

To stop automatic running later:

```sh
launchctl unload "$HOME/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist"
```

## Go From Dry Run To Paper Orders

After confirming the logs look right, set this in `config.json`:

```json
"dry_run": false
```

Keep `"paper": true` for simulated Alpaca orders. Only switch `"paper": false` if you intentionally want live Alpaca orders and understand the risk.

## Safely restart
```sh
launchctl unload ~/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist

launchctl load ~/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist

launchctl list | grep com.vaughnhuey.trend-bot
tail -n 20 logs/trend_bot.log
```

## Important Risk Note

Automated trading can lose money quickly. Paper trading is only a simulation and can differ from live trading because it does not fully reflect slippage, liquidity, fees, outages, or order-routing behavior.
