"""SQLite DB のバックアップスクリプト.

sqlite3 .backup API で物理コピー (WAL モードでも安全) → gzip → integrity_check 検証。
失敗時は exit code 1 で終了し、cron で Slack 通知連携想定。

Usage:
    python scripts/backup_db.py
    python scripts/backup_db.py --db data/kimochi.db --out data/backups/

Phase 0 では cron 設定はせず、手動実行のみ想定。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT / "data" / "kimochi.db"
DEFAULT_OUT = PROJECT / "data" / "backups"
RETENTION_DAYS = 7


def backup_db(db_path: Path, out_dir: Path) -> Path:
    """SQLite DB を gzip にバックアップ。失敗時は例外。"""
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"kimochi_{ts}.db.gz"

    # 一時ファイルに .backup
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(tmp_path))
        with dst:
            src.backup(dst)
        src.close()

        # integrity check
        check = dst.execute("PRAGMA integrity_check").fetchone()
        dst.close()
        if check is None or check[0] != "ok":
            raise RuntimeError(f"integrity_check failed: {check}")

        # gzip
        with open(tmp_path, "rb") as f_in, gzip.open(out_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        tmp_path.unlink(missing_ok=True)

    return out_path


def cleanup_old_backups(out_dir: Path, retention_days: int = RETENTION_DAYS) -> int:
    """retention_days より古いバックアップを削除。"""
    if not out_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    n_removed = 0
    for f in out_dir.glob("kimochi_*.db.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            n_removed += 1
    return n_removed


def main() -> int:
    parser = argparse.ArgumentParser(description="kimochi.db backup")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = parser.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out)

    try:
        out_path = backup_db(db_path, out_dir)
        print(f"OK: {out_path} ({out_path.stat().st_size:,} bytes)")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    n_removed = cleanup_old_backups(out_dir, args.retention_days)
    if n_removed:
        print(f"Cleanup: {n_removed} old backup(s) removed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
