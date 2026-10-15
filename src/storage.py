"""
Hybrid Leaner storage — SQLite trades + Parquet bars with 30-day rotation + gzipped external drive.

Keeps Mac lean (<100MB) while preserving data for nightly RL retrain and external archive.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pandas as pd

# ---- SQLite trades DB (replaces giant CSV) ----

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    notional REAL,
    qty REAL,
    fast_sma REAL,
    slow_sma REAL,
    rsi REAL,
    atr REAL,
    adx REAL,
    volume_ratio REAL,
    session_vwap REAL,
    breakout_price REAL,
    entry_type TEXT,
    regime_spy_trend TEXT,
    trade_duration_min REAL,
    mae REAL,
    mfe REAL,
    veto_prob REAL,
    rl_action TEXT,
    features_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
"""

VETO_REPLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS veto_replay (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    reason TEXT,
    veto_prob REAL,
    would_have_pnl REAL,
    would_have_pnl_pct REAL,
    features_json TEXT
);
"""

def init_trades_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(TRADES_SCHEMA)
        conn.executescript(VETO_REPLAY_SCHEMA)
        conn.commit()

def log_trade(db_path: Path, trade: dict[str, Any]) -> None:
    init_trades_db(db_path)
    # flatten features if dict
    features = trade.get("features_json")
    if isinstance(features, dict):
        trade = dict(trade)
        trade["features_json"] = json.dumps(features)
    cols = ["timestamp","symbol","action","price","reason","entry_price","exit_price","pnl","pnl_pct","notional","qty","fast_sma","slow_sma","rsi","atr","adx","volume_ratio","session_vwap","breakout_price","entry_type","regime_spy_trend","trade_duration_min","mae","mfe","veto_prob","rl_action","features_json"]
    vals = [trade.get(k) for k in cols]
    placeholders = ",".join(["?"]*len(cols))
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"INSERT INTO trades ({','.join(cols)}) VALUES ({placeholders})", vals)
        conn.commit()

def log_veto_replay(db_path: Path, replay: dict[str, Any]) -> None:
    init_trades_db(db_path)
    features = replay.get("features_json")
    if isinstance(features, dict):
        replay = dict(replay)
        replay["features_json"] = json.dumps(features)
    cols = ["timestamp","symbol","price","reason","veto_prob","would_have_pnl","would_have_pnl_pct","features_json"]
    vals = [replay.get(k) for k in cols]
    placeholders = ",".join(["?"]*len(cols))
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"INSERT INTO veto_replay ({','.join(cols)}) VALUES ({placeholders})", vals)
        conn.commit()

def query_trades(db_path: Path, limit: int = 1000, where: str = "") -> List[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        q = f"SELECT * FROM trades {where} ORDER BY timestamp DESC LIMIT {limit}"
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]

def get_training_frame(db_path: Path, limit: int = 5000) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(f"SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp DESC LIMIT {limit}", conn)
        return df

def vacuum_db(db_path: Path) -> None:
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")

# ---- Parquet bars (30-day rolling) ----

def write_bars_parquet(parquet_root: Path, symbol: str, day: date, bars: List) -> Path:
    """
    Write list[Bar] to data/parquet/YYYY-MM-DD/SYMBOL.parquet.gz
    Bars is list of Bar dataclass with timestamp, open, high, low, close, volume
    """
    day_str = day.isoformat()
    out_dir = parquet_root / day_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{symbol.replace('/','_')}.parquet"
    # convert to DataFrame
    rows = []
    for b in bars:
        rows.append({
            "timestamp": b.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return out_path
    df.to_parquet(out_path, compression="gzip", engine="pyarrow")
    return out_path

def read_bars_parquet(parquet_root: Path, symbol: str, day: date) -> List[dict]:
    path = parquet_root / day.isoformat() / f"{symbol.replace('/','_')}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path, engine="pyarrow")
    return df.to_dict(orient="records")

def list_parquet_days(parquet_root: Path) -> List[date]:
    if not parquet_root.exists():
        return []
    days = []
    for p in parquet_root.iterdir():
        if p.is_dir():
            try:
                days.append(date.fromisoformat(p.name))
            except Exception:
                continue
    return sorted(days)

def rotate_parquet(parquet_root: Path, archive_root: Path, keep_days: int = 30, gzipped_archive: bool = True) -> dict:
    """
    Keep only last keep_days locally, move older to archive/parquet/ (gzipped for external drive).
    Returns stats dict.
    """
    days = list_parquet_days(parquet_root)
    if len(days) <= keep_days:
        return {"kept": len(days), "archived": 0, "deleted": 0}
    to_archive = days[:-keep_days]
    archived = 0
    for d in to_archive:
        src = parquet_root / d.isoformat()
        if gzipped_archive:
            # move to archive_root as .tar.gz or keep parquet gz
            dst = archive_root / d.isoformat()
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
            # gzip each parquet file inside for extra external drive saving
            for parquet_file in dst.glob("*.parquet"):
                # already gzip compressed, just ensure .gz suffix for clarity? Keep as is
                # optionally create .gz wrapper
                gz_path = parquet_file.with_suffix(parquet_file.suffix + ".gz")
                # if not already gz, wrap
                if not parquet_file.suffix.endswith(".gz"):
                    # Parquet is already gzip-compressed internally, but we can add outer gzip for external
                    with open(parquet_file, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    parquet_file.unlink()
            archived += 1
        else:
            shutil.rmtree(src)
            archived += 1
    return {"kept": keep_days, "archived": archived, "deleted": 0}

def export_gzipped_archive(archive_root: Path, external_path: Path) -> dict:
    """
    Copy archive/parquet/*.parquet.gz to external drive path for later import.
    Keeps storage lean on Mac, allows re-import.
    """
    if not archive_root.exists():
        return {"copied": 0}
    external_path.mkdir(parents=True, exist_ok=True)
    copied = 0
    for day_dir in archive_root.iterdir():
        if not day_dir.is_dir():
            continue
        dst_day = external_path / day_dir.name
        dst_day.mkdir(parents=True, exist_ok=True)
        for f in day_dir.glob("*"):
            dst = dst_day / f.name
            shutil.copy2(f, dst)
            copied += 1
    return {"copied": copied, "external": str(external_path)}

def import_gzipped_archive(external_path: Path, parquet_root: Path) -> dict:
    """
    Re-import from external drive back to data/parquet for replay / retraining.
    Handles both .parquet and .parquet.gz
    """
    if not external_path.exists():
        return {"imported": 0}
    imported = 0
    for day_dir in external_path.iterdir():
        if not day_dir.is_dir():
            continue
        dst_day = parquet_root / day_dir.name
        dst_day.mkdir(parents=True, exist_ok=True)
        for f in day_dir.glob("*"):
            # if .parquet.gz outer gz, decompress inner parquet
            if f.suffix == ".gz" and ".parquet" in f.name:
                # decompress to parquet
                inner_name = f.name[:-3]  # remove .gz
                dst = dst_day / inner_name
                with gzip.open(f, "rb") as fin, open(dst, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                imported += 1
            else:
                dst = dst_day / f.name
                shutil.copy2(f, dst)
                imported += 1
    return {"imported": imported}

# ---- CSV migration helper ----

def migrate_csv_to_db(csv_path: Path, db_path: Path) -> int:
    """
    One-time migration of existing logs/trades.csv into SQLite for Hybrid Leaner.
    """
    import csv
    if not csv_path.exists():
        return 0
    init_trades_db(db_path)
    count = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Map csv cols to DB cols where possible
            trade = {
                "timestamp": row.get("timestamp") or row.get("entry_time"),
                "symbol": row.get("symbol"),
                "action": "sell" if row.get("exit_price") else "buy",
                "price": float(row.get("exit_price") or row.get("entry_price") or 0),
                "reason": row.get("exit_reason") or row.get("entry_type"),
                "entry_price": float(row.get("entry_price")) if row.get("entry_price") else None,
                "exit_price": float(row.get("exit_price")) if row.get("exit_price") else None,
                "pnl": float(row.get("pnl")) if row.get("pnl") else None,
                "pnl_pct": float(row.get("pnl_pct")) if row.get("pnl_pct") else None,
                "notional": float(row.get("notional_usd") or row.get("notional") or 25),
                "qty": float(row.get("qty") or 0),
                "fast_sma": float(row.get("fast_sma") or 0) if row.get("fast_sma") else None,
                "slow_sma": float(row.get("slow_sma") or 0) if row.get("slow_sma") else None,
                "rsi": None,
                "atr": float(row.get("atr_at_entry") or 0) if row.get("atr_at_entry") else None,
                "adx": float(row.get("adx_at_entry") or 0) if row.get("adx_at_entry") else None,
                "volume_ratio": None,
                "session_vwap": None,
                "breakout_price": None,
                "entry_type": row.get("entry_type"),
                "trade_duration_min": float(row.get("trade_duration_min") or 0),
                "mae": float(row.get("mae") or 0) if row.get("mae") else None,
                "mfe": float(row.get("mfe") or 0) if row.get("mfe") else None,
                "features_json": json.dumps(row),
            }
            log_trade(db_path, trade)
            count += 1
    return count
