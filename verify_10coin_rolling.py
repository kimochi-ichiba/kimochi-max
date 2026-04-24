"""
verify_10coin_rolling.py
========================
10通貨・12ヶ月ローリング検証 — 決定的な再現性テスト

目的:
  先ほどの3通貨検証では v95.0 の平均月次が -11.92% だった。
  しかし10通貨の1ヶ月検証では +29.78% と大幅に良い結果が出た。
  これが「再現性のある強さ」か「偶然」かを、10通貨で12ヶ月検証して結論を出す。
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
from datetime import datetime, timedelta

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from backtester import Backtester

logging.getLogger("data_fetcher").setLevel(logging.WARNING)
logging.getLogger("backtester").setLevel(logging.WARNING)

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT",
    "LINK/USDT", "AVAX/USDT", "DOT/USDT", "UNI/USDT", "NEAR/USDT"
]
WINDOW_DAYS = 30
STEP_DAYS = 15
HISTORY_DAYS = 365
INITIAL_BALANCE = 10_000.0


def run_single(symbol: str, start: str, end: str) -> dict:
    cfg = Config()
    bt = Backtester(cfg)
    r = bt.run(symbol, start, end, timeframe="1h", initial_balance=INITIAL_BALANCE)
    if not r.trades:
        return {"trades": 0, "pnl": 0.0, "final": INITIAL_BALANCE, "pnl_pct": 0.0, "wins": 0}
    wins = len([t for t in r.trades if t.won])
    return {
        "trades": len(r.trades),
        "wins": wins,
        "pnl": r.final - r.initial,
        "final": r.final,
        "pnl_pct": (r.final / r.initial - 1) * 100,
    }


def run_window(start: str, end: str, win_idx: int, total: int) -> dict:
    per_sym = {}
    for sym in SYMBOLS:
        per_sym[sym] = run_single(sym, start, end)

    total_pnl = sum(r["pnl"] for r in per_sym.values())
    total_initial = INITIAL_BALANCE * len(SYMBOLS)
    port_return = total_pnl / total_initial * 100
    total_trades = sum(r["trades"] for r in per_sym.values())
    total_wins = sum(r["wins"] for r in per_sym.values())
    win_rate = total_wins / total_trades * 100 if total_trades else 0

    # 画面出力
    per_returns = " ".join(f"{per_sym[s]['pnl_pct']:+5.0f}" for s in SYMBOLS)
    print(f"  [{win_idx:2d}/{total}] {start} 〜 {end}  "
          f"{port_return:+7.2f}% "
          f"取引{total_trades:3d} 勝率{win_rate:4.1f}%  "
          f"[{per_returns}]")

    return {
        "start": start, "end": end,
        "port_return": port_return,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "per_sym": per_sym,
    }


def main():
    end_date = datetime(2026, 4, 18)
    start_from = end_date - timedelta(days=HISTORY_DAYS)
    windows = []
    cursor = start_from
    while cursor + timedelta(days=WINDOW_DAYS) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=WINDOW_DAYS)
        windows.append((w_s.strftime("%Y-%m-%d"), w_e.strftime("%Y-%m-%d")))
        cursor += timedelta(days=STEP_DAYS)

    print(f"\n🔬 10通貨・12ヶ月ローリング検証 (v95.0現コード)")
    print(f"{'='*92}")
    print(f"期間: {start_from.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"ウィンドウ: {len(windows)}個 (30日 × 15日刻み)")
    print(f"銘柄: {', '.join(SYMBOLS)}")
    print(f"各銘柄初期資金: ${INITIAL_BALANCE:,.0f} (合計ポートフォリオ: ${INITIAL_BALANCE*len(SYMBOLS):,.0f})")
    print(f"{'='*92}")
    header_sym = " ".join(f"{s.split('/')[0]:>5s}" for s in SYMBOLS)
    print(f"  {'Win#':4s} {'期間':27s} {'月次':>7s} {'取引数':>6s} {'勝率':>6s}  [{header_sym}]")
    print(f"  {'-'*92}")

    results = []
    for i, (s, e) in enumerate(windows, 1):
        r = run_window(s, e, i, len(windows))
        results.append(r)

    rets = np.array([r["port_return"] for r in results])

    print(f"\n{'='*92}")
    print(f"  📊 10通貨ポートフォリオ 集計統計")
    print(f"{'='*92}")
    print(f"  ウィンドウ数           : {len(rets)}")
    print(f"  平均月次リターン       : {np.mean(rets):+.2f}%")
    print(f"  中央値                 : {np.median(rets):+.2f}%")
    print(f"  最高                   : {np.max(rets):+.2f}%")
    print(f"  最低                   : {np.min(rets):+.2f}%")
    print(f"  標準偏差               : {np.std(rets):.2f}%")
    print(f"  プラス月               : {sum(1 for r in rets if r > 0)}/{len(rets)} ({sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%)")
    print(f"  +30%以上               : {sum(1 for r in rets if r >= 30)}/{len(rets)}")
    print(f"  +20%以上               : {sum(1 for r in rets if r >= 20)}/{len(rets)}")
    print(f"  +10%以上               : {sum(1 for r in rets if r >= 10)}/{len(rets)}")
    print(f"  -10%以下               : {sum(1 for r in rets if r <= -10)}/{len(rets)}")

    # 比較
    print(f"\n  📈 3通貨版 vs 10通貨版")
    print(f"  {'='*70}")
    print(f"  {'戦略':<25s} {'平均月次':>10s} {'勝率':>8s} {'最高':>8s} {'最低':>8s}")
    print(f"  {'-'*70}")
    print(f"  {'3通貨 (BTC/ETH/SOL)':<25s} {'-11.92%':>10s} {'22%':>8s} {'+82.5%':>8s} {'-55.0%':>8s}")
    print(f"  {'10通貨 (分散版)':<25s} {f'{np.mean(rets):+.2f}%':>10s} "
          f"{f'{sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%':>8s} "
          f"{f'{np.max(rets):+.1f}%':>8s} {f'{np.min(rets):+.1f}%':>8s}")
    print(f"  {'='*70}")

    # 最終判定
    avg = np.mean(rets)
    pos_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
    print(f"\n  🎯 最終判定:")
    print(f"  {'='*70}")
    if avg >= 15 and pos_rate >= 60:
        print(f"  ✅ 10通貨版は再現性あり！月+{avg:.1f}%、勝率{pos_rate:.0f}%で実運用価値あり。")
    elif avg >= 5 and pos_rate >= 50:
        print(f"  ⚠️ プラスだが月+30%には届かない。月+{avg:.1f}%、勝率{pos_rate:.0f}%。")
    elif avg > 0:
        print(f"  ⚠️ プラスだが弱い。月+{avg:.1f}%、勝率{pos_rate:.0f}%。運用は慎重に。")
    else:
        print(f"  ❌ 10通貨でも不採算。月{avg:+.1f}%、勝率{pos_rate:.0f}%。戦略変更が必要。")
    print(f"  {'='*70}\n")


if __name__ == "__main__":
    main()
