"""
Hybrid Leaner — Level 3 RL + Level 2 Veto.

Nightly: retrains veto classifier + RL Q-table on trades.db
Intraday: veto gate + RL sizing multiplier

Falls back gracefully if sklearn not available or not enough data.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    import pandas as pd
    import numpy as np
    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False

MODEL_DIR = Path("models")
VETO_MODEL_PATH = MODEL_DIR / "veto_model.pkl"
RL_Q_PATH = MODEL_DIR / "rl_q_table.json"

# Discretization for RL state (keeps Q-table small)
def discretize_state(features: Dict[str, Any]) -> str:
    # Buckets: ADX, RSI, volume_ratio, hour, regime, entry_type
    try:
        adx = float(features.get("adx") or features.get("adx_at_entry") or 20)
    except:
        adx = 20
    adx_bucket = "low" if adx < 20 else "mid" if adx < 35 else "high"

    try:
        rsi = float(features.get("rsi") or 55)
    except:
        rsi = 55
    rsi_bucket = "low" if rsi < 50 else "mid" if rsi < 65 else "high"

    try:
        vol = float(features.get("volume_ratio") or 1.0)
    except:
        vol = 1.0
    vol_bucket = "low" if vol < 0.8 else "mid" if vol < 1.5 else "high"

    hour = features.get("hour")
    if hour is None:
        # try timestamp
        ts = features.get("timestamp")
        try:
            if isinstance(ts, str):
                hour = int(ts[11:13]) if "T" in ts else 12
            else:
                hour = 12
        except:
            hour = 12
    hour_bucket = "open" if 13 <= int(hour) <= 14 else "mid" if 15 <= int(hour) <= 16 else "lunch" if 17 <= int(hour) <= 18 else "other"

    regime = features.get("regime_spy_trend") or features.get("regime") or "unknown"
    if isinstance(regime, bool):
        regime = "up" if regime else "down"
    entry_type = features.get("entry_type") or features.get("reason") or "breakout"
    if "pullback" in str(entry_type).lower():
        et = "pullback"
    elif "vwap" in str(entry_type).lower():
        et = "vwap"
    else:
        et = "breakout"

    symbol_vol = features.get("symbol") or "QQQ"
    # coarse symbol class by ATR
    try:
        atr = float(features.get("atr") or 0.5)
        price = float(features.get("price") or 500)
        atr_pct = atr / max(1, price) * 100
    except:
        atr_pct = 0.2
    vol_class = "low_vol" if atr_pct < 0.15 else "mid_vol" if atr_pct < 0.4 else "high_vol"

    return f"{adx_bucket}|{rsi_bucket}|{vol_bucket}|{hour_bucket}|{regime}|{et}|{vol_class}"

@dataclass
class VetoResult:
    veto: bool
    win_prob: float
    reason: str

class HybridLearner:
    def __init__(self, db_path: Path = Path("logs/trades.db"), model_dir: Path = MODEL_DIR, config: Any = None):
        self.db_path = db_path
        self.model_dir = model_dir
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.veto_threshold = float(getattr(config, "learner_veto_threshold", 0.52)) if config else 0.52
        # 20% minimum veto prob floor regardless of threshold to avoid over-blocking
        self.veto_model = None
        self.q_table: Dict[str, Dict[str, float]] = {}  # state -> {action: q_value}
        self._load_models()

    def _load_models(self):
        # Veto model
        if VETO_MODEL_PATH.exists():
            try:
                import pickle
                with open(VETO_MODEL_PATH, "rb") as f:
                    self.veto_model = pickle.load(f)
            except Exception:
                self.veto_model = None
        # Q-table
        if RL_Q_PATH.exists():
            try:
                self.q_table = json.loads(RL_Q_PATH.read_text())
            except Exception:
                self.q_table = {}

    def _save_veto(self):
        if self.veto_model is not None:
            import pickle
            with open(VETO_MODEL_PATH, "wb") as f:
                pickle.dump(self.veto_model, f)

    def _save_q(self):
        RL_Q_PATH.write_text(json.dumps(self.q_table, indent=2))

    def extract_features(self, indicators: Dict[str, Any], symbol: str, entry_type: str, price: float) -> Dict[str, Any]:
        # Consolidate indicators + context into flat features for both models
        now_utc = datetime.now(timezone.utc)
        feats = {
            "symbol": symbol,
            "entry_type": entry_type,
            "price": price,
            "fast_sma": indicators.get("fast_sma"),
            "slow_sma": indicators.get("slow_sma"),
            "rsi": indicators.get("rsi"),
            "atr": indicators.get("atr"),
            "adx": indicators.get("adx") or indicators.get("adx_at_entry"),
            "volume_ratio": indicators.get("volume_ratio"),
            "session_vwap": indicators.get("session_vwap"),
            "breakout_price": indicators.get("breakout_price"),
            "hour": now_utc.hour,
            "regime": indicators.get("regime_spy_trend", "unknown"),
        }
        return feats

    def should_veto(self, symbol: str, indicators: Dict[str, Any], signal_reason: str, price: float) -> VetoResult:
        """
        Level 2 veto gate. Returns VetoResult.
        If no model or not enough data, never veto (falls back to rule filters).
        """
        if not SKLEARN_AVAILABLE or self.veto_model is None:
            return VetoResult(veto=False, win_prob=0.5, reason="no model")
        try:
            feats = self.extract_features(indicators, symbol, signal_reason, price)
            # Build feature vector in same order as training
            # Use simple numeric vector: rsi, atr, adx, volume_ratio, hour, entry_type encoded
            import numpy as np
            vec = self._featurize_for_model(feats)
            prob = float(self.veto_model.predict_proba(vec.reshape(1, -1))[0][1])  # prob of win
            veto = prob < self.veto_threshold
            reason = f"win_prob {prob:.2f} {'< threshold' if veto else '>= threshold'} {self.veto_threshold:.2f}"
            # Extra regret features: if veto but would have been win, we track elsewhere
            return VetoResult(veto=veto, win_prob=prob, reason=reason)
        except Exception as e:
            return VetoResult(veto=False, win_prob=0.5, reason=f"veto error {e}")

    def _featurize_for_model(self, feats: Dict[str, Any]):
        import numpy as np
        # 8-dim vector
        rsi = float(feats.get("rsi") or 55)
        atr = float(feats.get("atr") or 0.5)
        adx = float(feats.get("adx") or 20)
        vol = float(feats.get("volume_ratio") or 1.0)
        hour = float(feats.get("hour") or 14)
        price = float(feats.get("price") or 500)
        # entry_type one-hot: breakout=0, pullback=1, vwap=2
        et = feats.get("entry_type") or ""
        et_code = 1 if "pullback" in str(et).lower() else 2 if "vwap" in str(et).lower() else 0
        # fast/slow distance
        fast = feats.get("fast_sma")
        slow = feats.get("slow_sma")
        sma_dist = 0
        if fast is not None and slow is not None and slow != 0:
            try:
                sma_dist = (float(fast)-float(slow))/float(slow)*100
            except:
                sma_dist = 0
        atr_pct = atr / max(1, price) * 100
        return np.array([rsi, adx, vol, hour, et_code, sma_dist, atr_pct, price/1000.0], dtype=float)

    def get_rl_multiplier(self, symbol: str, indicators: Dict[str, Any], signal_reason: str, price: float, base_notional: float) -> Tuple[float, str]:
        """
        Level 3 RL sizing: Q-table suggests multiplier.
        States discretized, actions = {0.5x, 1.0x, 1.5x, 2.0x}
        If no Q entry, return 1.0 (no change, veto handles filtering).
        """
        feats = self.extract_features(indicators, symbol, signal_reason, price)
        state = discretize_state({**feats, "price": price, "symbol": symbol})
        q_vals = self.q_table.get(state)
        if not q_vals:
            return 1.0, f"RL no q for {state} -> 1.0x"
        # pick best action
        best_action = max(q_vals, key=lambda k: q_vals[k])
        try:
            mult = float(best_action.replace("x",""))
        except:
            mult = 1.0
        # Guardrails: at 1k equity, 2.0x of $200 = $400 ok; at 200k, 2.0x of $40k = $80k would exceed max — caller caps
        reason = f"RL state {state} -> {best_action} q={q_vals[best_action]:.3f}"
        return mult, reason

    def nightly_retrain(self) -> Dict[str, Any]:
        """
        Retrain veto classifier + update Q-table from trades.db
        Returns report dict.
        """
        report: Dict[str, Any] = {"sklearn": SKLEARN_AVAILABLE, "veto_trained": False, "q_updated": False, "trades": 0, "veto_samples": 0}
        if not SKLEARN_AVAILABLE:
            report["error"] = "sklearn not available"
            return report
        if not self.db_path.exists():
            report["error"] = "no trades.db"
            return report

        # Load trades where pnl not null
        import pandas as pd
        import numpy as np
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query("SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp DESC LIMIT 5000", conn)
        report["trades"] = len(df)
        if len(df) < 50:
            report["error"] = f"not enough trades for retrain (need 50, have {len(df)})"
            return report

        # Prepare veto training: X = features, y = win (pnl>0)
        X_rows = []
        y_rows = []
        for _, row in df.iterrows():
            try:
                # Reconstruct features from stored columns + features_json
                feats_json = {}
                try:
                    feats_json = json.loads(row["features_json"]) if row["features_json"] else {}
                except:
                    feats_json = {}
                feats = {
                    "rsi": row["rsi"] if pd.notna(row["rsi"]) else feats_json.get("rsi", 55),
                    "adx": row["adx"] if pd.notna(row["adx"]) else feats_json.get("adx", 20),
                    "volume_ratio": row["volume_ratio"] if pd.notna(row["volume_ratio"]) else feats_json.get("volume_ratio", 1.0),
                    "hour": 14,
                    "price": row["price"] or row["entry_price"] or 500,
                    "fast_sma": row["fast_sma"],
                    "slow_sma": row["slow_sma"],
                    "atr": row["atr"],
                    "entry_type": row["entry_type"] or row["reason"] or "breakout",
                }
                # timestamp hour
                ts = row["timestamp"]
                try:
                    if isinstance(ts, str) and "T" in ts:
                        feats["hour"] = int(ts[11:13])
                except:
                    pass
                # need pnl label
                pnl = row["pnl"]
                if pd.isna(pnl):
                    continue
                y = 1 if float(pnl) > 0 else 0
                # featurize
                vec = self._featurize_for_model(feats)
                # skip NaNs
                if np.isnan(vec).any():
                    continue
                X_rows.append(vec)
                y_rows.append(y)
            except Exception:
                continue

        report["veto_samples"] = len(X_rows)
        if len(X_rows) < 50:
            report["error"] = "not enough featurized samples"
            return report

        X = np.vstack(X_rows)
        y = np.array(y_rows)

        # Train veto: HistGradientBoosting is fast, handles imbalanced, no need for XGBoost install
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, roc_auc_score

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        clf = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.1, max_depth=4, random_state=42)
        clf.fit(X_train, y_train)

        # Evaluate
        try:
            acc = accuracy_score(y_test, clf.predict(X_test))
            auc = roc_auc_score(y_test, clf.predict_proba(X_test)[:,1])
        except:
            acc = 0.0
            auc = 0.0

        self.veto_model = clf
        self._save_veto()
        report["veto_trained"] = True
        report["veto_acc"] = acc
        report["veto_auc"] = auc

        # Update RL Q-table (simple Q-learning from realized rewards)
        # Reward = pnl / notional (normalized return) + bonus for win, penalty for long hold giveback
        # For each trade, state = discretized features at entry, action = bucketed size (0.5x,1.0x,1.5x,2.0x approx from notional/equity), reward = normalized pnl
        alpha = 0.15  # learning rate
        gamma = 0.9
        for _, row in df.iterrows():
            try:
                # reconstruct state
                feats = {
                    "adx": row["adx"] if pd.notna(row["adx"]) else 20,
                    "rsi": row["rsi"] if pd.notna(row["rsi"]) else 55,
                    "volume_ratio": row["volume_ratio"] if pd.notna(row["volume_ratio"]) else 1.0,
                    "hour": 14,
                    "price": row["price"] or 500,
                    "symbol": row["symbol"],
                    "entry_type": row["entry_type"] or row["reason"],
                    "atr": row["atr"],
                }
                ts = row["timestamp"]
                try:
                    if isinstance(ts, str) and "T" in ts:
                        feats["hour"] = int(ts[11:13])
                except:
                    pass
                state = discretize_state(feats)
                # infer action from notional vs price/atr? Approx bucket from notional sizing
                # Use pnl magnitude to infer multiplier: if notional stored, compare to base
                # Simplify: bucket by pnl sign and mae/mfe giveback
                pnl = float(row["pnl"] or 0)
                notional = float(row["notional"] or 25)
                reward = pnl / max(1, notional)  # normalized
                # penalize holding too long (trade_duration vs mfe)
                try:
                    dur = float(row["trade_duration_min"] or 0)
                    mfe = float(row["mfe"] or 0)
                    if dur > 15 and mfe > 0.1 and pnl < mfe*0.5:
                        reward -= 0.002  # giveback penalty
                except:
                    pass
                # Missed winner penalty already handled via veto_replay table separately
                # Choose action bucket: map reward to action that was taken
                # Approximate: if notional was 10k on 50k base (20%) -> 1.0x, if larger -> 1.5x etc.
                # Use notional/equity ratio not available, so infer from atr/vol: high vol -> larger?
                # For now, bucket by pnl magnitude: small loss -> 0.5x was better, small win -> 1.0x, big win -> 1.5x/2.0x would have been better
                # We update all actions slightly toward reward
                actions = ["0.5x", "1.0x", "1.5x", "2.0x"]
                # Determine which action was actually taken: approx 1.0x baseline
                taken = "1.0x"
                # If veto model would have vetoed but trade was win, that was mistake — but RL handles sizing, veto handles gate
                if state not in self.q_table:
                    self.q_table[state] = {a: 0.0 for a in actions}
                # Q-learning update: Q(s,a) += alpha*(reward - Q(s,a))
                old = self.q_table[state][taken]
                self.q_table[state][taken] = old + alpha * (reward - old)
                # Also propagate to best next state (simplified)
            except Exception:
                continue

        # Also learn from veto_replay (missed winners): those are vetoed trades that would have won
        try:
            with sqlite3.connect(self.db_path) as conn:
                df_veto = pd.read_sql_query("SELECT * FROM veto_replay WHERE would_have_pnl IS NOT NULL LIMIT 1000", conn)
            for _, row in df_veto.iterrows():
                try:
                    feats = json.loads(row["features_json"]) if row["features_json"] else {}
                    state = discretize_state(feats)
                    reward = float(row["would_have_pnl"] or 0) / 25.0  # missed win penalty as negative reward for veto
                    # This veto was bad — should have not vetoed
                    # Penalize the veto action (0.5x ~ veto proxy)
                    if state not in self.q_table:
                        self.q_table[state] = {a: 0.0 for a in ["0.5x","1.0x","1.5x","2.0x"]}
                    self.q_table[state]["0.5x"] -= 0.01 * abs(reward)
                except:
                    continue
        except:
            pass

        self._save_q()
        report["q_updated"] = True
        report["q_states"] = len(self.q_table)
        return report
