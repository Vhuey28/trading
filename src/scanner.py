"""
Open Scanner — discovers hot symbols from universe, ranks, proposes rotation via Telegram.

Hybrid Leaner B superpower: auto-finds hotter symbols when current ones go flat.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any
from zoneinfo import ZoneInfo

from src.models import Bar

# Default liquid universe if no watchlist provided (top liquid ETFs + mega-cap stocks)
DEFAULT_WATCHLIST = [
    "QQQ","SPY","IWM","DIA","TQQQ","SQQQ","SPXL","SPXS","NVDA","AAPL","TSLA","MSFT","META","AMZN","GOOGL","AVGO","MU","AMD","NFLX","SMH","XLF","XLE","XLK","GLD","SLV","TLT","HYG","EFA","EEM",
    "VOO","VTI","SCHD","BND","ARKK","SOXL","SOXS","AAL","COIN","MSTR","PLTR","ARM","QCOM","TSM","ASML","COST","LLY","JPM","GS","BTC-USD"
]

@dataclass
class SymbolScore:
    symbol: str
    score: float
    momentum_5d: float
    atr_pct: float
    volume_ratio: float
    vwap_distance: float
    win_prob: float | None  # from learner veto model if available
    reason: str

def compute_momentum(bars: List[Bar]) -> float:
    if len(bars) < 5:
        return 0.0
    closes = [b.close for b in sorted(bars, key=lambda b: b.timestamp)]
    try:
        return (closes[-1] / closes[-5] - 1.0) * 100.0
    except:
        return 0.0

def compute_atr_pct(bars: List[Bar]) -> float:
    if len(bars) < 15:
        return 0.0
    # simple ATR% last 14
    try:
        from src.indicators import atr
        a = atr(sorted(bars, key=lambda b: b.timestamp), 14)
        price = bars[-1].close
        return (a / max(1, price)) * 100.0 if a else 0.0
    except:
        return 0.0

def compute_volume_ratio(bars: List[Bar], period: int = 20) -> float:
    if len(bars) < period:
        return 1.0
    try:
        from src.indicators import avg_volume
        avg = avg_volume(bars, period)
        last = bars[-1].volume
        return last / max(1, avg) if avg else 1.0
    except:
        return 1.0

def score_symbol(bars: List[Bar], veto_prob: float | None = None) -> SymbolScore:
    mom = compute_momentum(bars)
    atrp = compute_atr_pct(bars)
    volr = compute_volume_ratio(bars)
    # distance from VWAP
    try:
        from src.indicators import vwap
        vw = vwap(bars)
        dist = (bars[-1].close / max(1, vw) - 1.0)*100 if vw else 0.0
    except:
        dist = 0.0

    # Composite score: momentum 40% + ATR 20% + vol 20% + trend alignment 20%
    # Prefer high momentum + high ATR (profit opportunity) + volume confirmation
    score = 0
    score += mom * 2.0  # 5d momentum weighted
    score += atrp * 3.0  # high ATR = more profit per trade
    score += (volr - 1.0) * 5.0
    score += max(0, dist) * 1.0  # above VWAP bullish
    # veto prob boost: if learner says 62% win, add
    if veto_prob is not None:
        score += (veto_prob - 0.5) * 20.0

    reason = f"mom{mom:.1f}% atr{atrp:.1f}% vol{volr:.1f}x vwap{dist:+.1f}%"
    if veto_prob is not None:
        reason += f" win{veto_prob:.0%}"
    return SymbolScore(symbol="", score=score, momentum_5d=mom, atr_pct=atrp, volume_ratio=volr, vwap_distance=dist, win_prob=veto_prob, reason=reason)

class OpenScanner:
    def __init__(self, config, bot):
        self.config = config
        self.bot = bot
        self.watchlist = getattr(config, "scanner_watchlist", None) or DEFAULT_WATCHLIST
        self.top_n = int(getattr(config, "scanner_top_n", 5))
        self.min_volume = int(getattr(config, "scanner_min_volume", 500000))
        self.pending_path = Path(getattr(config, "state_file", "state/trend_bot_state.json")).parent / "pending_rotation.json"
        # legacy path
        if "trend_bot_state" in str(self.pending_path):
            self.pending_path = self.pending_path.parent / "pending_rotation.json"

    def rank_candidates(self) -> List[SymbolScore]:
        """
        Rank watchlist by fetching recent bars and scoring.
        Requires Alpaca creds; if not available, returns watchlist order.
        """
        from src.indicators import atr
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        import logging
        logger = self.bot.logger if hasattr(self.bot, "logger") else None
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        end = now.astimezone(timezone.utc)
        start = end - timedelta(days=7)  # 5 trading days
        scored: List[SymbolScore] = []

        # Try to use learner veto prob if available
        learner = getattr(self.bot, "learner", None)

        for symbol in self.watchlist[:100]:  # cap scan to 100 to respect rate limits
            try:
                # Use bot's fetch to respect feed/adjustment
                bars = self.bot._fetch_bars(symbol, start, end, "1Min")  # fallback to daily if 1Min too much?
                # If 1Min returns huge (7 days * 390 = 2730 bars), ok; if empty, try daily via Stocks txt?
                if not bars:
                    # try daily bars via fetch with 1Day?
                    try:
                        bars = self.bot._fetch_bars(symbol, start, end, "1Day")
                    except:
                        continue
                if len(bars) < 10:
                    continue
                # filter min volume (avg)
                avg_vol = sum(b.volume for b in bars[-20:]) / max(1, min(20, len(bars)))
                if avg_vol < self.min_volume:
                    continue
                # get veto win_prob if learner exists
                win_prob = None
                if learner and hasattr(learner, "should_veto"):
                    # mock indicators for scoring
                    fake_ind = {
                        "rsi": 55, "adx": 25, "atr": atr(sorted(bars, key=lambda b: b.timestamp), 14) if len(bars)>15 else 0.5,
                        "volume_ratio": compute_volume_ratio(bars),
                        "fast_sma": 0, "slow_sma": 0, "session_vwap": 0,
                    }
                    try:
                        res = learner.should_veto(symbol, fake_ind, "breakout entry", bars[-1].close)
                        win_prob = res.win_prob
                    except:
                        win_prob = None

                sc = score_symbol(bars, win_prob)
                sc.symbol = symbol
                scored.append(sc)
            except Exception as e:
                if logger:
                    logger.debug("[Scanner] skip %s: %s", symbol, e)
                continue

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:20]  # top 20 for inspection

    def evaluate_rotation(self, current_symbols: List[str]) -> Dict[str, Any] | None:
        """
        Compare current portfolio vs top ranked.
        Returns proposal dict if rotation beneficial, else None.
        Logic: if worst current symbol's score < best candidate's score * 0.7, propose swap.
        """
        ranked = self.rank_candidates()
        if not ranked:
            return None
        # score current symbols too
        # Build dict of current scores
        current_scores: Dict[str, SymbolScore] = {}
        for sym in current_symbols:
            # find in ranked or score separately
            match = next((s for s in ranked if s.symbol == sym), None)
            if match:
                current_scores[sym] = match
            else:
                # not in top 20, treat as low score
                current_scores[sym] = SymbolScore(symbol=sym, score=-999, momentum_5d=0, atr_pct=0, volume_ratio=0, vwap_distance=0, win_prob=None, reason="not in top 20")

        # Find worst current
        worst_sym = min(current_scores, key=lambda k: current_scores[k].score)
        worst_score = current_scores[worst_sym].score
        best_candidate = ranked[0]
        # If best candidate not already held and is significantly better
        if best_candidate.symbol in current_symbols:
            # find best not held
            for cand in ranked:
                if cand.symbol not in current_symbols:
                    best_candidate = cand
                    break
            else:
                return None

        # Threshold: best must beat worst by 30% or PF logic
        # Also check trades.db PF for worst
        try:
            pf_worst = self._get_symbol_pf(worst_sym)
        except:
            pf_worst = 1.0

        should_rotate = (best_candidate.score > worst_score * 1.3) or (pf_worst < 0.9 and best_candidate.score > 10)

        if not should_rotate:
            return None

        return {
            "drop": {"symbol": worst_sym, "score": worst_score, "reason": current_scores[worst_sym].reason, "pf": pf_worst},
            "add": {"symbol": best_candidate.symbol, "score": best_candidate.score, "reason": best_candidate.reason, "win_prob": best_candidate.win_prob},
            "ranked": [{"symbol": s.symbol, "score": round(s.score,1), "reason": s.reason} for s in ranked[:10]],
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        }

    def _get_symbol_pf(self, symbol: str) -> float:
        # Query trades.db for last 20 trades PF
        try:
            import sqlite3
            db_path = Path(getattr(self.config, "storage_db_path", "logs/trades.db"))
            if not db_path.exists():
                db_path = Path("logs/trades.db")
            if not db_path.exists():
                return 1.0
            with sqlite3.connect(db_path) as conn:
                cur = conn.execute("SELECT pnl FROM trades WHERE symbol=? AND pnl IS NOT NULL ORDER BY timestamp DESC LIMIT 20", (symbol,))
                pnls = [r[0] for r in cur.fetchall() if r[0] is not None]
            if not pnls:
                return 1.0
            gross_profit = sum(p for p in pnls if p>0)
            gross_loss = abs(sum(p for p in pnls if p<0))
            if gross_loss == 0:
                return 999 if gross_profit>0 else 1.0
            return gross_profit / gross_loss
        except:
            return 1.0

    def propose_via_telegram(self, proposal: Dict[str, Any]) -> bool:
        """
        Send Telegram proposal with details of both symbols.
        Writes pending_rotation.json for later approval.
        """
        drop = proposal["drop"]
        add = proposal["add"]
        pending = {
            "proposal": proposal,
            "status": "pending",
            "created_at": proposal["timestamp"],
        }
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(json.dumps(pending, indent=2))

        msg = (
            f"🔄 *Symbol Rotation Proposal*\n"
            f"*DROP* `{drop['symbol']}` — score {drop['score']:.1f} PF {drop['pf']:.2f}\n"
            f"  {drop['reason']}\n"
            f"*ADD* `{add['symbol']}` — score {add['score']:.1f} win_prob {add['win_prob']:.0% if add['win_prob'] is not None else 'n/a'}\n"
            f"  {add['reason']}\n"
            f"Top 5 ranked: {', '.join([r['symbol'] for r in proposal['ranked'][:5]])}\n"
            f"Reply `/approve {add['symbol']}` or `/reject` — or edit `{self.pending_path}`\n"
            f"Sandbox: first 5 trades in new symbol at 50% size (10% equity) until PF>1.2"
        )
        try:
            # Use existing notifier
            ok = self.bot.notifier.send_message(msg) if hasattr(self.bot, "notifier") else False
            self.bot.logger.info("Scanner proposal sent via Telegram: drop %s -> add %s", drop["symbol"], add["symbol"])
            return bool(ok)
        except Exception as e:
            self.bot.logger.warning("Telegram proposal failed: %s", e)
            return False

    def check_approval(self) -> Dict[str, Any] | None:
        """
        Check if pending rotation was approved via Telegram command file or manual edit.
        Returns proposal if approved, None otherwise.
        Approval via: user sends Telegram /approve SYMBOL which bot polling writes to pending file as status=approved
        Or manual: edit pending_rotation.json status to approved.
        """
        if not self.pending_path.exists():
            return None
        try:
            data = json.loads(self.pending_path.read_text())
            status = data.get("status")
            if status == "approved":
                proposal = data.get("proposal")
                # clear pending after reading
                # keep file but mark done?
                # we archive to approved
                approved_path = self.pending_path.with_suffix(".approved.json")
                approved_path.write_text(json.dumps(data, indent=2))
                self.pending_path.unlink()
                return proposal
            if status == "rejected":
                self.pending_path.unlink()
                return None
            # pending — check timeout (24h auto-reject)
            created = data.get("created_at")
            try:
                if created:
                    from src.utils import parse_alpaca_timestamp
                    ctime = parse_alpaca_timestamp(created)
                    if (datetime.now(timezone.utc) - ctime).total_seconds() > 86400:
                        self.pending_path.unlink()
                        return None
            except:
                pass
            return None
        except:
            return None

    def apply_approved_rotation(self, proposal: Dict[str, Any], current_symbols: List[str]) -> List[str]:
        """
        Apply approved rotation: drop worst, add candidate.
        Updates config symbols in-memory and writes to state for persistence.
        Caller should persist to config.json or state.
        """
        drop_sym = proposal["drop"]["symbol"]
        add_sym = proposal["add"]["symbol"]
        new_list = [s for s in current_symbols if s != drop_sym]
        if add_sym not in new_list:
            new_list.append(add_sym)
        self.bot.logger.info("Scanner rotation APPLIED: dropped %s, added %s -> %s", drop_sym, add_sym, new_list)
        # Also log to trades.db as event?
        return new_list
