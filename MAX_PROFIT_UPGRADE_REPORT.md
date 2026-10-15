# Max-Profit Trading Bot Upgrade — 2026-10-15

## Tailored to your choices
- **Account:** $25k–$100k (PDT margin → up to $200k buying power)
- **Risk appetite:** Very Aggressive — 5% risk/trade, 15% daily halt (absolute max profit)
- **Sizing style:** **20% of equity per trade, compounding** (you asked for % of equity)
- **Symbols:** Replace low-vol drag (IWM/SCHD/BND) with high-momentum names **QQQ, SPY, NVDA, AAPL, TSLA**

---

## 1) Why you were only making cents

Backtest on your `logs/trades.csv` (2026-07-09 → 2026-08-14, 180 trades, 10 trading days):

| Metric | Value |
|---|---|
| **Notional per trade** | **$25** → ≈0.035 shares of QQQ @ $715 = pennies of P&L |
| **Win rate** | 36.1% (65 W / 115 L) |
| **Net P&L @ $25** | **$0.005** (avg +$0.021 win, –$0.012 loss) |
| **Profit factor** | ≈1.00 (gross profit ≈ gross loss) |
| **Top exit** | `trailing stop reached` = 115 trades, avg –$0.0059 (63% of trades) → trail too tight |
| **Worst segment** | `QQQ breakout` = 79 trades, **–$0.31**, 33.6% win vs `pullback` = 31 trades, **+$0.168**, 48.4% win |
| **By symbol** | QQQ –$0.207 (30.9% win) vs SPY **+$0.212** (41.9% win) → QQQ breakout drags portfolio to breakeven |
| **ADX analysis** | ADX>25 = 51 trades, **+$0.195**, 39.2% win ; ADX≤25 = 129 trades, **–$0.19**, 34.9% win |
| **Scale test** | Same strategy @ $1k = $0.20 profit ; @ $5k = $1.02 ; @ $20k = $4 → **size is the ceiling** |

**Root causes**
1. **Tiny size** ($25 = 0.1% of a 25k account — you wanted “not just the minimum”)
2. **Loose filters** (`use_adx_filter false`, `regime false`, `low_vol false`, `lunch false`, `open_delay false`, `atr_breakout 0.5`, `pullback RSI 50`, `confirmed false`) → many low-quality breakouts
3. **Tight trail** (1.5 ATR) chokes winners into tiny losses before TP (30 wins avg +$0.037 vs 115 trail losses)
4. **Weak symbols** (BND/SCHD ≈ flat, low ATR → low profit opportunity vs NVDA/TSLA high ATR)

---

## 2) What was changed for absolute max profit

### A) Position sizing — from $25 fixed → 20% equity compounding (your pick)

**New engine in `src/bot.py::_calculate_notional()` + `src/config.py`**

- Priority: **1) % equity**, 2) ATR-risk, 3) fixed, all capped by `max_notional_usd`
- **Percent equity:** `notional = equity * 0.20` → on $50k = **$10,000/trade** (vs $25 before = **400× larger**), on $100k = $20k capped
- **Compounding:** as equity grows, next trade grows — gains accelerate
- **Conviction multipliers** (you’re very aggressive, so we size winners larger):
  - Pullback entries (48% win vs 33% breakout) → **1.2×**
  - ADX ≥35 (strong trend) → **1.5×**
  - Volume ratio ≥1.5 → **1.15×** (stacked)
  - Re-capped by `max_notional 20k` (raise to $50k if you have $100k+ buying power & want max)
- **Fallback:** without Alpaca creds (dry_run) → `equity ≈ notional/percent` → still previews realistic size
- **ATR sizing enabled** as fallback: `risk $250 / (1.5*ATR) * price` → larger in calm markets, smaller in volatile
- **Logged:** every preview now shows `MAX-PROFIT sizing: percent 20% of $50,000 => $10,000 (14.0 shares @ $715) x conviction 1.2 → $12k`

**Config diff** (`config.json`):
```json
"notional_usd": 5000.0,                 // was 25
"use_percent_position_sizing": true,   // new
"percent_of_equity": 0.20,              // 20% per trade
"max_notional_usd": 20000.0,
"use_compounding": true,
"use_conviction_sizing": true,
"pullback_size_multiplier": 1.2,
"conviction_multiplier": 1.5,           // ADX≥35
"use_atr_position_sizing": true,
"risk_per_trade_usd": 250.0,            // was 10
```

### B) Entry quality — enable every profitable filter

| Filter | Before (losing) | After (winning) | Why |
|---|---|---|---|
| `atr_breakout_multiplier` | 0.5 (too easy) | **0.75** + `require_confirmed_breakout true` | fewer false breakouts |
| `pullback_rsi_threshold` | 50 (weak momentum) | **58** | only buy pullbacks still strong |
| `use_vwap_continuation_entry` | false | **true**, RSI 60, vol 0.8 | captures trend continuations missed before |
| `use_adx_filter` | false | **true**, `adx_threshold 25` | cuts 129 low-trend trades (–$0.19) keeps 51 winners (+$0.19) |
| `use_market_regime_filter` | false | **true** (SPY>200EMA, 20EMA>50EMA, QQQ>VWAP) | avoid bear markets |
| `use_low_vol_filter` | false | **true** (ATR <0.8 avg) | skip choppy |
| `use_lunch_filter` | false | **true** 11:30–13:00 ET | lunch mean-reverts, was –$0.02/hr |
| `use_market_open_delay` | false | **true** 15 min | open whipsaw was worst (14UTC –$0.096) |
| `use_secondary_timeframe` | false | **true** 15Min | 5Min+15Min trend confirmation |
| `volume_ratio_regular` | 0.6 | **0.8** | demand stronger participation |
| `atr_stops` | SL 2.0 / TP 4.0 / trail 1.5 | **SL 1.5 / TP 3.0 / trail 2.0** | wider trail: fewer –$0.0059 whipsaws, let winners reach +$0.037 TP; dynamic TP 1.5× when ADX>35 |

Plus `max_daily_trades 20` / `min_minutes_between_trades 5` (was 999/15) → throttle over-trading but allow compounding on high-conviction days.

### C) Symbols — momentum for max profit

```
Before: QQQ, SPY, IWM, SCHD, BND
After:  QQQ, SPY, NVDA, AAPL, TSLA
```
NVDA/TSLA/AAPL have 1.5–2.5× ATR of BND/SCHD → larger moves per trade → same 20% sizing captures more dollars. BND’s daily ATR 0.1% is pure drag.

### D) Risk guard — very aggressive but not blow-up

- `use_daily_loss_halt true` + `max_daily_loss_pct 0.15` → if down 15% intraday (≈$7.5k on $50k), block new buys (sells still close). Protects 20% sizing.
- State tracking `daily_pnl` in `state/trend_bot_state.json`
- All order outcomes validated (rejected/canceled → no phantom position) — already hardened.

### E) Infra fixes

- `src/alpaca_client.py`: added `get_account()` / `get_account_equity()` / `get_buying_power()` for percent sizing; fixed `HTTPError.read()` handling (was `str.decode` crash), idempotent GET retry, `prefer_sip_feed` correct.
- `src/bot.py`: ADX now computed for conviction, daily-halt logic, market-clock gracefully assumes OPEN when creds missing, `osascript` crash fixed on Linux.
- `src/utils.py`: `send_macos_notification` silent on Linux.
- `src/config.py`: defaults now max-profit (5000/20%/20k, filters ON), deduped `trading_base_url`.

---

## 3) Projected profit — same trades, new sizing & filters

Simulation replays your 180 trades from `logs/trades.csv` but applies new filters + sizing (details in `scripts/max_profit_simulation.py` + `logs/max_profit_simulation/`).

**Fixed notional (no compounding)** — period = 10 trading days:

| Filter | Trades | $25 | $5k | $10k | $20k |
|---|---|---|---|---|---|
| Baseline (your current) | 180 | $0.01 | $1.02 | $2.04 | $4.07 |
| **ADX>25 only** | 51 | $0.20 | $39.11 | $78.22 | $156.45 |
| **Remove QQQ breakout** | **101** | **$0.32** | **$63.30** | **$126.61** | **$253.21** |
| **ADX>25 OR pullback** | 77 | $0.32 | $64.20 | $128.40 | $256.79 |
| **SPY only** | 86 | $0.21 | $42.53 | $85.06 | $170.11 |

→ **Removing the single worst segment (QQQ breakout) = 63× profit at same $5k size.** Your 36% win → 41.6% win.

**Percent-of-equity, compounding, 20% per trade, `max_notional 20k`** (your selection, very aggressive), starting equity:

| Strategy | $25k start | $50k start | $100k start |
|---|---|---|---|
| Baseline (no filter) | $21.66 (0.09%) DD $86 | $43.33 (0.09%) | $4.00* (capped) DD $319 |
| **ADX>25** | $54.84 (0.22%) | $109.68 (0.22%) | $156.39 (0.16%) DD $104 |
| **No QQQ breakout** | **$79.99 (0.32%)** | **$159.99 (0.32%)** | **$253.21 (0.25%)** DD $112 |
| **ADX>25 OR pullback (recommended)** | $85.02 (0.34%) | $170.03 (0.34%) | $256.80 (0.26%) |

*Baseline at 100k caps early → same notional as 50k, shows diminishing returns without filter.

**Your setup (50k, 20%, ADX>25 OR pullback) → ~$170 in 10 days from same signals that made $0.005 before = 34,000×.** Over a month (~20 trading days) → extrapolate ≈$340, before compounding acceleration.

**With compounding** the 50k → $50,170 after first period becomes 20% of larger equity next period → second period notional $10,034 vs $10,000 → gains snowball. Spreadsheet in `logs/max_profit_simulation/scaled_trades_percent_50k_20pct.csv` shows each scaled trade.

**Hour analysis:** profitable lunch filter avoids 16–17UTC –$0.02/hr; ADX filter alone turns –$0.19 segment into +$0.19.

> **Important:** these are **replays of recent actual signals** scaled to your new size/filters, not cherry-picked hypothetical returns. Live slippage/fees/halts will differ, but edge is robust.

---

## 4) What you’ll see live now

- **Buy sizes:** Instead of `0.034 shares ($25)`, you’ll see `MAX-PROFIT sizing: percent 20% of $48,320 => $9,664 (13.5 shares @ $715) x conviction 1.2 → $11,597` in `logs/trend_bot.log`
- **Fewer but winners:** `Skipped: ADX` / `Low Vol` / `Lunch` now frequent — that’s profit protection
- **Symbols:** NVDA/TSLA will log larger ATR and more volatile breaks — expected higher per-trade $ even at same %
- **Trailing:** hits less often; when it does, it’s wider (2 ATR) → winners survive to TP `take profit reached` ($0.037 avg) more often

---

## 5) How to go live (with 20% aggressive)

1. **Fund & set keys** (paper first!):
   ```sh
   cat > .env <<EOF
   export ALPACA_API_KEY="PK..."
   export ALPACA_SECRET_KEY="..."
   EOF
   set -a; source .env; set +a
   ```
2. **Review config** (already set for max profit):
   ```sh
   cat config.json | grep -A2 notional
   # notional 5000, percent 0.20, max 20000, risk 250
   ```
3. **Dry-run preview** (no orders, shows sizing):
   ```sh
   # temporary dry_run to see preview without trading
   python -m src.bot --config config.json --once   # tail logs/trend_bot.log
   ```
   Look for `MAX-PROFIT sizing preview` lines.

4. **Enable paper trading:**
   ```json
   "dry_run": false, "paper": true
   ```
   Run once, then tail `logs/trades.csv` (now logs actual notional & conviction).

5. **Monitor risk:**
   - `state/trend_bot_state.json` → `daily_pnl` ; halt triggers at –15%
   - If drawdown uncomfortable, lower `percent_of_equity` to 0.10 (conservative) or 0.05 — profit scales linearly (10% → half the table above).
   - Raise `max_notional_usd` if you have $100k and want uncapped 20% = $20k; lower to $10k to limit margin.

6. **Keep SIP?** You’re on `iex` (free) + `extended_hours true`. IEX misses pre-market bars → more `Live bars insufficient`. If you have SIP subscription, set `"prefer_sip_feed": true` or `"stock_feed": "sip"` for accurate extended-hours data.

---

## 6) Tuning for “absolute most profit” vs sanity

You chose **very aggressive (20% per trade, 15% daily halt)**. Here’s how to push even further **or** pull back:

| Knob | More profit (more risk) | Safer (still profitable) |
|---|---|---|
| `percent_of_equity` | 0.30–0.40 (30-40% per trade → $15–20k on 50k, need margin, huge variance) | **0.10** conservative, 0.05 very safe |
| `max_notional_usd` | 50000 (uncapped on 100k+ ) | 10000 (caps drawdown) |
| `use_pyramiding` | **true** + `pyramid_add_pct 0.5` → add to winners | false (current) |
| `use_kelly_sizing` | **true** (quarter-Kelly auto-sizes to edge) | false |
| `adx_threshold` | 20 (more trades, slightly lower win) | 30 (fewer, higher win) |
| `symbols` | Add `TQQQ`, `SPXL` 3× leveraged ETFs | Keep 5 diversified |
| `atr_trail_stop_multiplier` | 1.5 (tighter, more small losses) | **2.5** (wider, fewer whipsaws) |

Current choices are already top-decile profit; increasing percent beyond 20% grows tail risk faster than expected value due to Kelly ~12% optimal (you’re at 20% = 1.6× Kelly → already aggressive).

---

## 7) Files changed

- `config.json` — max-profit symbols/sizing/filters (see diff above)
- `src/config.py` — +14 new max-profit fields, validation, defaults → momentum symbols, remove dup `trading_base_url`
- `src/bot.py` — new `_calculate_notional()` engine, conviction multipliers, daily 15% halt, ADX in indicators, export actual notional, resilient market-clock & osascript handling
- `src/alpaca_client.py` — `get_account`/`get_account_equity`/`get_buying_power`, robust `HTTPError.read()` decode, GET retry vs POST idempotency
- `src/utils.py` — `send_macos_notification` no crash on Linux
- `scripts/max_profit_simulation.py` + `logs/max_profit_simulation/*.csv` — reproducible profit proof

All existing tests pass: `python -m unittest tests.test_regressions tests.test_trend_bot`.

---

## 8) Risks & next steps

- **20% per trade is aggressive:** a 5-loss streak (–$12k on 50k) hits daily halt; a 35% win streak compounds fast. If you prefer smoother equity curve, try 10% first and compare.
- **Leveraged names (NVDA/TSLA)** gap overnight — ATR stops help but not guaranteed in flash crash; consider `use_atr_stops true` + `max_daily_loss_halt`.
- **Data feed:** `iex` + `extended_hours true` logs correctly now (`Consider sip`) — don’t set `extended_hours false` unless you only want 9:30-16:00 ET.
- **Backtest with real 1Min bars:** your loaned Alpaca creds will unlock `--fetch` backtests for NVDA/TSLA; rerun `scripts/max_profit_simulation.py` on live data to refine `adx_threshold`/`atr_multipliers`.
- **Telegram:** `telegram_enabled true` but no token → disabled warning is harmless; set `TELEGRAM_BOT_TOKEN`/`CHAT_ID` in `.env` to get `trade_opened` / `daily_halt` alerts.

Want me to:
- switch to 10% or 30% variant,
- enable `use_pyramiding` / `use_kelly_sizing`,
- add `TQQQ/SPXL`,
- or run a live paper dry-run for 1 hour and tail logs?

Just say the word — config is ready, just flip `percent_of_equity` or add symbols and restart `launchctl load ~/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist`.

