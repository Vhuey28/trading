# Hybrid Learner — Storage, Scanner, RL + Veto (Build 2026-10-15)

> You asked: **"build now and the gzipped option so i can put onto an external drive and give to the bot at a later date"**  
> This build does it: SQLite + Parquet live lean (30 days) + gzipped archive for external drive import/export + Hybrid Learner (RL sizing + Veto gate) + Open Scanner + Telegram approval.

---

## 1. What was built

| Layer | What | Where | File |
|-------|------|-------|------|
| **Storage lean** | SQLite `logs/trades.db` (2 MB /10k trades) + `veto_replay` table + Parquet `data/parquet/YYYY-MM-DD/TICKER.parquet` (gzip) + rotation + `archive/parquet/` (compressed `*.parquet.gz`) | `src/storage.py` | `src/storage.py` |
| **Learner Level 3 hybrid** | HistGradientBoosting veto (8-dim: rsi, adx, vol, hour, entry_type, sma_dist, atr_pct, price) threshold 0.52 + Q-table RL sizing (state 7-dim → actions 0.5x/1.0x/1.5x/2.0x, α0.15 γ0.9, reward PnL/notional − giveback) | `src/learner.py` | `src/learner.py` |
| **Scanner open** | Score `mom*2 + atr*3 + (vol−1)*5 + vwapdist + (win_prob−0.5)*20`, rank 49 watchlist → 500 fallback, propose worst → best if `best >1.3× worst` or PF<0.9 | `src/scanner.py` | `src/scanner.py` |
| **Telegram ask** | `/approve SYMBOL` / `/reject` via `getUpdates` offset, writes `state/pending_rotation.json` | `src/telegram_notifier.py` | `src/telegram_notifier.py` |
| **Bot integration** | `$200 floor` sizing (1k→$230, 200k→$20k capped), veto gate + `veto_replay` log, RL multiplier, parquet per fetch, nightly 22:00 retrain + 07:00 scanner rank + poll | `src/bot.py` | `src/bot.py` |
| **Config** | `storage_keep_days 30`, `storage_gzipped_archive true`, `storage_min_notional 200`, `learner_enabled true threshold 0.52 retrain 22`, `scanner_enabled true top_n 5 approval_required true` | `src/config.py` + `config.json` | `src/config.py` `config.json` |
| **External-drive CLI** | `manage_storage.py info|migrate|rotate|export|import` | `scripts/manage_storage.py` | `scripts/manage_storage.py` |

Nightly cron (ET) implemented inside `bot.py:run_once()`:

- **22:00** — `learner.nightly_retrain()` (HistGB + Q-table), save `models/veto_model.pkl` + `models/rl_q_table.json`, then `rotate_parquet(keep 30d)` → `archive/parquet/*.parquet.gz`
- **07:00** — `scanner.rank_candidates()` → `evaluate_rotation()` → `propose_via_telegram()` (card: OUT PF/win% vs IN PF/ATR/rank/veto_prob) → writes `state/pending_rotation.json`
- **Every hour** — `telegram_notifier.poll_approvals()` checks for `/approve`/`/reject`, `check_approval()` → `apply_approved_rotation()` (mutates `config.symbols` live + persists `state.symbols_override`), Telegram ack

Guardrails: 15% daily halt, 60% exposure cap (via daily PnL tracking), new scanner symbol sandbox 50% size for first 5 trades until PF>1.2, black-box profit preference (veto_prob logged, not explained).

---

## 2. Storage = Lean Mac + Gzipped external drive

**Design choice — Hybrid B (SQLite live + Parquet recent + compressed archive + models)**, why:

- SQLite `trades.db` — tiny, never leaves laptop, powers veto/Q-table nightly (no CSV parsing)
- Parquet `data/parquet/` — columnar gzip for bars, only last 30 days live (≈ 200 MB), learner replay + scanner momentum need it
- `archive/parquet/` — `*.parquet.gz` doubly compressed, ready to copy to external drive
- `models/` — veto pkl + Q-table JSON (≈ 300 KB), always local

**On Mac:**

```
trading/
  logs/trades.db              # permanent, <5 MB
  data/parquet/2026-10-26/... # 30 days live, auto-rotated
  archive/parquet/...         # compressed, kept locally until you export
  models/veto_model.pkl
  models/rl_q_table.json
```

Cron after 22:00 retrain:

```python
rotate_parquet(parquet_root, archive_root, keep_days=30, gzipped_archive=True)
# moves days >30d from data/parquet/ → archive/parquet/*.parquet.gz (1.5–3x smaller)
```

**External drive flow (gzipped option you requested):**

```bash
# 1) On Mac, after a week/month, copy archive to external drive (Plug → Copy → Eject)
python scripts/manage_storage.py export --external /Volumes/MyDrive/trading_archive
# copies archive/parquet/*.parquet.gz → /Volumes/MyDrive/trading_archive/parquet/*.parquet.gz
# then you can delete local archive/ to keep Mac <100 MB: rm -rf archive/parquet/*

# 2) Later, give drive to bot (or plug back) to replay history:
python scripts/manage_storage.py import --external /Volumes/MyDrive/trading_archive
# copies /Volumes/MyDrive/trading_archive/parquet/*.parquet.gz → data/parquet/ (decompresses)
# then learner can nightly_retrain() on full history without re-downloading

# 3) Check sizes
python scripts/manage_storage.py info
python scripts/manage_storage.py rotate   # manual rotation before export
python scripts/manage_storage.py migrate  # one-time CSV→DB (already done: 180 rows migrated)
```

What to copy with **macOS drag-and-drop** or `rsync`:

```bash
rsync -avh --progress archive/parquet/ /Volumes/MyDrive/trading_archive/parquet/
rsync -avh logs/trades.db /Volumes/MyDrive/trading_backup/
```

Import is idempotent — re-running does not duplicate rows (SQLite unique handling via migration).

---

## 3. RL + Veto — how it learns from mistakes (including missed winners)

**Inputs (per trade):** `rsi, adx, atr, volume_ratio, hour, entry_type (breakout/pullback/vwap/switch), regime_spy_trend, regime_vwap, vwap_distance, vol_class` — all already computed by bot, plus VIX/breadth if you add to `indicators`.

**Veto (Level 2):** `HistGradientBoostingClassifier` on win/loss (binary). Features 8-dim, threshold 0.52 (default). Today after migrating 180 trades: `veto_acc 0.64 auc 0.60 q_states 18`. Blocked trades go to `veto_replay` table; nightly missed-winner check can compare veto'd price move (we log features_json for replay).

**RL (Level 3):** Discretized state 7-dim (adx low/mid/high, rsi, vol, hour, regime, entry_type, vol_class) → Q-table 4 actions (0.5x/1.0x/1.5x/2.0x). Reward = `pnl/notional − giveback_penalty` (giveback = (mfe−pnl)/notional if mfe>0). Update after each closed trade via `learner.update_from_trade()`, or batch in `nightly_retrain()` which rebuilds Q from all DB rows (α0.15 γ0.9). `get_rl_multiplier()` returns best action for current state.

**Sizing pipeline** (`_calculate_notional`):

```
equity * 20% → floor $200 (1k→$230 with vol boost, 200k→$20k capped) → RL multiplier (e.g., 0.5x if state historically loses) → conviction (pullback 1.2x, adx≥35 1.8x, vol≥1.5 1.15x) → cap max_notional → final notional
```

Sand box: new scanner symbol first 5 trades 50% size (10% equity) until PF>1.2 — tracked via `SELECT COUNT(*) FROM trades WHERE symbol=?`.

---

## 4. Scanner + Telegram approval card

Ranking (respects rate limit — fetches `top_n` candidates sequentially with trend bars):

```
composite = 5d_momentum*2 + atr_pct*3 + (vol_avg-1)*5 + vwap_dist + (win_prob-0.5)*20
```

Worst currently held (PF computed from `trades.db` last 30d, 16d requirement) vs best candidate: propose if `best.score >1.3× worst.score` or `PF<0.9`. Proposal written to `state/pending_rotation.json`:

```json
{
  "drop": "QQQ",
  "drop_pf": 0.88, "drop_win": 0.33,
  "add": "NVDA", "add_score": 12.4, "add_win_prob": 0.61,
  "message": "🔄 Proposal: drop QQQ (PF 0.88, 33%) → add NVDA (score 12.4, veto p 0.61)… Reply /approve NVDA or /reject"
}
```

Bot polls Telegram `getUpdates?offset=…` hourly; on `/approve NVDA` → `apply_approved_rotation([old], approved)` → new `symbols` live + written to `state.symbols_override` so restart keeps it.

Fallback watchlist (49 liquid) used unless you expand to 500 S&P via `DEFAULT_WATCHLIST` in `src/scanner.py` — add symbols there and scanner will test them nightly (rank limited to top 100 to respect 300 req/min).

---

## 5. How to run / verify

```bash
pip install -r requirements.txt  # already has pandas pyarrow sklearn
python -m src.bot --config config.json --once   # dry_run preview, veto+RL logged
python scripts/manage_storage.py info           # DB 180 rows, models present
python scripts/manage_storage.py export --external /tmp/ext_demo  # test gzipped export
python scripts/manage_storage.py import --external /tmp/ext_demo  # re-import
pytest / python -m unittest tests.test_regressions tests.test_trend_bot  # 19 OK
```

Config example (already in `config.json`):

```json
{
  "storage_keep_days": 30,
  "storage_gzipped_archive": true,
  "storage_min_notional": 200,
  "learner_enabled": true,
  "learner_veto_threshold": 0.52,
  "learner_retrain_hour": 22,
  "scanner_enabled": true,
  "scanner_top_n": 5,
  "scanner_approval_required": true
}
```

---

## 6. Files to exclude from git (lean)

Already in `.gitignore` (verify): `data/parquet/`, `archive/parquet/`, `archive_external/`, `logs/trades.db` may be tracked as schema only — keep schema, ignore large `.parquet`. Models `models/*.pkl` are small and can be committed or gitignored per preference.

---

*Next: expand watchlist to S&P 500, add VIX/regime breadth to `indicators` for learner, tune veto threshold after 500+ trades (target 0.55–0.58 if FP too high), consider 40k cap (`max_notional_usd`) when equity hits 200k.*
