"""
daily_snapshot.py
=================
ペーパートレード1ヶ月分の日次スナップショット記録システム

機能:
- 毎日1回、ボット状態を JSON に記録
- 時系列で総資産・ポジション・PnL を追跡
- 30日後にレポート自動生成

実行方法:
    python3 daily_snapshot.py          # 今日の状態を記録
    python3 daily_snapshot.py --report  # 集計レポート表示
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import ccxt


STATE_FILE = "kelly_bot_state.json"
SNAPSHOT_DIR = Path("snapshots")


def take_snapshot() -> dict:
    """現在の状態をスナップショット"""
    state_path = Path(STATE_FILE)
    if not state_path.exists():
        return {"error": "ボット未起動"}

    state = json.loads(state_path.read_text())
    positions = state.get("positions", {})

    total_unrealized = 0
    pos_snapshot = []
    try:
        ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
        for sym, pos in positions.items():
            try:
                ticker = ex.fetch_ticker(sym)
                current = float(ticker["last"])
                entry = pos["entry_price"]
                size = pos["size"]
                unrealized = (current - entry) * size
                total_unrealized += unrealized
                pos_snapshot.append({
                    "symbol": sym,
                    "entry_price": entry,
                    "current_price": current,
                    "size": size,
                    "leverage": pos["leverage"],
                    "unrealized": unrealized,
                    "margin": pos.get("initial_margin", 0),
                })
            except Exception as e:
                pos_snapshot.append({"symbol": sym, "error": str(e)})
    except Exception as e:
        return {"error": f"API失敗: {e}"}

    total_cap = state.get("total_capital", 0)
    start_cap = state.get("start_capital", 3000)
    cash = total_cap - sum(pos.get("initial_margin", 0) for pos in positions.values())
    total_equity = cash + sum(p.get("margin", 0) + p.get("unrealized", 0) for p in pos_snapshot)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "total_capital": total_cap,
        "total_equity": total_equity,
        "cash": cash,
        "total_unrealized": total_unrealized,
        "pnl": total_equity - start_cap,
        "pnl_pct": (total_equity / start_cap - 1) * 100 if start_cap > 0 else 0,
        "positions": pos_snapshot,
        "last_rebalance": state.get("last_rebalance", ""),
    }


def save_snapshot():
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    snap = take_snapshot()
    if "error" in snap:
        print(f"⚠️ {snap['error']}")
        return

    # 日付別ファイル (同日複数回実行で上書き)
    filename = SNAPSHOT_DIR / f"snap_{snap['date']}.json"
    filename.write_text(json.dumps(snap, indent=2, ensure_ascii=False))
    print(f"✅ スナップショット保存: {filename}")
    print(f"   総資産: ${snap['total_equity']:,.2f}  PnL: ${snap['pnl']:+,.2f} ({snap['pnl_pct']:+.2f}%)")


def load_all_snapshots() -> list:
    if not SNAPSHOT_DIR.exists(): return []
    snapshots = []
    for p in sorted(SNAPSHOT_DIR.glob("snap_*.json")):
        try:
            snapshots.append(json.loads(p.read_text()))
        except Exception:
            pass
    return snapshots


def report():
    """30日レポート生成"""
    snaps = load_all_snapshots()
    if not snaps:
        print("スナップショットがありません")
        return

    print(f"\n{'='*70}")
    print(f"  📊 Paper Trading 日次レポート ({len(snaps)}日分)")
    print(f"{'='*70}")

    start_cap = 3000  # 初期資金
    print(f"  {'日付':12s} {'総資産':>12s} {'損益':>12s} {'損益率':>8s} {'現金':>10s}")
    print(f"  {'-'*60}")
    for s in snaps:
        date = s.get("date", "")
        eq = s.get("total_equity", 0)
        pnl = s.get("pnl", 0)
        pnl_pct = s.get("pnl_pct", 0)
        cash = s.get("cash", 0)
        pnl_str = f"${pnl:+,.2f}"
        print(f"  {date:12s}  ${eq:>9,.2f}  {pnl_str:>11s}  {pnl_pct:+6.2f}%  ${cash:>7,.2f}")

    # 統計
    if len(snaps) >= 2:
        first, last = snaps[0], snaps[-1]
        delta = last["total_equity"] - first["total_equity"]
        print(f"\n  📈 期間統計:")
        print(f"    最初の日 : {first['date']} → ${first['total_equity']:,.2f}")
        print(f"    最後の日 : {last['date']} → ${last['total_equity']:,.2f}")
        print(f"    変動    : ${delta:+,.2f} ({(last['total_equity']/first['total_equity']-1)*100:+.2f}%)")

        # 最高・最低
        max_s = max(snaps, key=lambda x: x.get("total_equity", 0))
        min_s = min(snaps, key=lambda x: x.get("total_equity", 0))
        print(f"    最高    : {max_s['date']} → ${max_s['total_equity']:,.2f}")
        print(f"    最低    : {min_s['date']} → ${min_s['total_equity']:,.2f}")

    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="集計レポート表示")
    args = parser.parse_args()
    if args.report:
        report()
    else:
        save_snapshot()
