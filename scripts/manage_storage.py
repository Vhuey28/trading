#!/usr/bin/env python3
"""
Manage Hybrid Leaner storage — lean local + gzipped external drive.

Usage:
  python scripts/manage_storage.py info
  python scripts/manage_storage.py export --external /Volumes/MyDrive/trading_archive
  python scripts/manage_storage.py import --external /Volumes/MyDrive/trading_archive
  python scripts/manage_storage.py rotate
  python scripts/manage_storage.py migrate
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.storage import (
    init_trades_db,
    migrate_csv_to_db,
    rotate_parquet,
    export_gzipped_archive,
    import_gzipped_archive,
)
from src.config import BotConfig

def main():
    parser = argparse.ArgumentParser(description="Manage Hybrid Leaner storage")
    parser.add_argument("command", choices=["info","export","import","rotate","migrate"], help="Action")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--external", default="archive_external", help="External drive path for export/import")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = BotConfig.load(cfg_path.resolve())
    else:
        cfg = BotConfig()

    db_path = ROOT / getattr(cfg, "storage_db_path", "logs/trades.db")
    parquet_root = ROOT / getattr(cfg, "storage_parquet_root", "data/parquet")
    archive_root = ROOT / getattr(cfg, "storage_archive_root", "archive/parquet")
    external = Path(args.external)
    if not external.is_absolute():
        external = ROOT / external

    if args.command == "info":
        import sqlite3
        print("=== Storage Info ===")
        print(f"DB: {db_path} exists={db_path.exists()} size={db_path.stat().st_size/1024:.1f}KB" if db_path.exists() else f"DB: {db_path} not found")
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                cnt = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                vcnt = conn.execute("SELECT COUNT(*) FROM veto_replay").fetchone()[0]
                print(f"  trades rows: {cnt}")
                print(f"  veto_replay rows: {vcnt}")
        print(f"Parquet root: {parquet_root} exists={parquet_root.exists()}")
        if parquet_root.exists():
            days = sorted([p.name for p in parquet_root.iterdir() if p.is_dir()])
            total_files = sum(1 for _ in parquet_root.rglob("*.parquet*"))
            total_size = sum(f.stat().st_size for f in parquet_root.rglob("*") if f.is_file()) / 1024 / 1024
            print(f"  days kept locally: {len(days)} ({days[:3]} ... {days[-3:]})")
            print(f"  parquet files: {total_files} total {total_size:.1f} MB")
        print(f"Archive root: {archive_root} exists={archive_root.exists()}")
        if archive_root.exists():
            adays = sorted([p.name for p in archive_root.iterdir() if p.is_dir()])
            atotal = sum(f.stat().st_size for f in archive_root.rglob("*") if f.is_file()) / 1024 / 1024
            print(f"  archived days: {len(adays)} ({adays[:3]} ... {adays[-3:]}) total {atotal:.1f} MB")
        print(f"External example: {external}")
        print(f"Models: {ROOT / 'models'} exists={(ROOT/'models').exists()} files={list((ROOT/'models').glob('*')) if (ROOT/'models').exists() else []}")
        print("\nLean settings:")
        print(f"  keep_days={getattr(cfg, 'storage_keep_days', 30)} gzipped={getattr(cfg, 'storage_gzipped_archive', True)} min_notional={getattr(cfg, 'storage_min_notional', 200)}")

    elif args.command == "migrate":
        csv_path = ROOT / getattr(cfg, "trades_csv_path", "logs/trades.csv")
        n = migrate_csv_to_db(csv_path, db_path)
        print(f"Migrated {n} rows from {csv_path} to {db_path}")

    elif args.command == "rotate":
        stats = rotate_parquet(parquet_root, archive_root, keep_days=int(getattr(cfg, "storage_keep_days", 30)), gzipped_archive=bool(getattr(cfg, "storage_gzipped_archive", True)))
        print(f"Rotated: {stats}")

    elif args.command == "export":
        stats = export_gzipped_archive(archive_root, external)
        print(f"Exported {stats.get('copied',0)} files to {stats.get('external', external)}")
        print("You can now safely copy this folder to your external drive and delete local archive to free space.")

    elif args.command == "import":
        stats = import_gzipped_archive(external, parquet_root)
        print(f"Imported {stats.get('imported',0)} files from {external} to {parquet_root}")
        print("Bot can now replay those days for retraining.")

if __name__ == "__main__":
    main()
