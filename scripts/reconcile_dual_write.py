"""dual-write 突合スクリプト (Phase 6 監視用).

JSON (results/demo_state.json) と DB (snapshots テーブル最新行) の差分を検証。
- equity の差 > 0.01% で警告
- positions の数量差で警告
- 失敗時 exit code 1 で終了 (cron で Slack/Discord 通知連携想定)

Usage:
    python scripts/reconcile_dual_write.py
    python scripts/reconcile_dual_write.py --tolerance-pct 0.05  # 緩める
    python scripts/reconcile_dual_write.py --json    # JSON 出力 (cron parse 用)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from db.repositories.state_repo import read_latest_snapshot

DEFAULT_STATE_PATH = PROJECT / "results" / "demo_state.json"
DEFAULT_DB_PATH = PROJECT / "data" / "kimochi.db"


def _load_json_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _diff_pct(a: float, b: float) -> float:
    if max(abs(a), abs(b)) == 0:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b))


def reconcile(
    json_state: dict,
    db_snapshot: dict,
    tolerance_pct: float = 0.0001,
) -> dict:
    """JSON と DB の最新 state を突合。差分 dict を返す."""
    issues: list[dict[str, Any]] = []

    json_eq = float(json_state.get("total_equity") or 0)
    db_eq = float(db_snapshot.get("equity") or 0)
    eq_diff = _diff_pct(json_eq, db_eq)
    if eq_diff > tolerance_pct:
        issues.append({
            "kind": "equity_mismatch",
            "json": json_eq, "db": db_eq,
            "diff_pct": eq_diff,
            "tolerance": tolerance_pct,
        })

    # positions 数の比較
    json_btc_pos = bool(json_state.get("btc_part", {}).get("position"))
    json_ach_count = len(json_state.get("ach_part", {}).get("positions", {}))
    json_total_pos = (1 if json_btc_pos else 0) + json_ach_count

    db_pos_list = db_snapshot.get("positions", [])
    db_total_pos = len(db_pos_list)

    if json_total_pos != db_total_pos:
        issues.append({
            "kind": "position_count_mismatch",
            "json": json_total_pos, "db": db_total_pos,
        })

    # ts のずれ
    json_ts = json_state.get("last_update")
    db_ts_ms = db_snapshot.get("ts")
    return {
        "json_equity": json_eq,
        "db_equity": db_eq,
        "equity_diff_pct": eq_diff,
        "json_positions": json_total_pos,
        "db_positions": db_total_pos,
        "json_last_update": json_ts,
        "db_ts_ms": db_ts_ms,
        "issues": issues,
        "ok": len(issues) == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="dual-write reconciliation")
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--mode", default="sim", choices=["sim", "backtest"])
    parser.add_argument("--tolerance-pct", type=float, default=0.0001,
                        help="許容差 (0.0001 = 0.01%)")
    parser.add_argument("--json", action="store_true", help="JSON 出力 (parse 用)")
    args = parser.parse_args()

    json_state = _load_json_state(Path(args.state_path))
    db_snapshot = read_latest_snapshot(mode=args.mode, db_path=args.db)

    if json_state is None and db_snapshot is None:
        print("ERROR: both JSON and DB are empty", file=sys.stderr)
        return 2
    if json_state is None:
        print("WARN: JSON state not found", file=sys.stderr)
        return 1
    if db_snapshot is None:
        print("WARN: DB has no snapshots", file=sys.stderr)
        return 1

    result = reconcile(json_state, db_snapshot, tolerance_pct=args.tolerance_pct)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"JSON equity:  ${result['json_equity']:,.2f}")
        print(f"DB equity:    ${result['db_equity']:,.2f}")
        print(f"diff:         {result['equity_diff_pct']*100:.4f}%")
        print(f"JSON positions: {result['json_positions']}")
        print(f"DB positions:   {result['db_positions']}")
        if result["ok"]:
            print("OK: no issues")
        else:
            print(f"\n⚠️ {len(result['issues'])} issue(s):")
            for i in result["issues"]:
                print(f"  - {i}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
