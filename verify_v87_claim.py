"""
verify_v87_claim.py
===================
スクリーンショットに記録された「v87.0時代、10銘柄×1ヶ月で月+6.9%」の主張を検証。

元の主張:
- 期間: 2026-03-17 〜 2026-04-17 (1ヶ月)
- 銘柄: BTC, ETH, SOL, BNB, ADA, LINK, AVAX, DOT, UNI, NEAR の10通貨
- 結果: 16取引、勝率37.5%、PF=1.22、月利+6.9%、P&L +$6,871
- 各銘柄に$10,000相当を割り当てたと思われる（総PnL/10銘柄で想像）

現在のコードはv95.0なので、v87.0と完全には一致しないが、
同じパラメータで回した場合に近い結果が出るかを確認する。
"""

from __future__ import annotations

import sys
import logging
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")

from config import Config
from backtester import Backtester

logging.getLogger("data_fetcher").setLevel(logging.WARNING)
logging.getLogger("backtester").setLevel(logging.WARNING)

# スクリーンショットの10銘柄
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT",
    "LINK/USDT", "AVAX/USDT", "DOT/USDT", "UNI/USDT", "NEAR/USDT"
]
START = "2026-03-17"
END = "2026-04-17"
INITIAL_BALANCE = 10_000.0


def run_symbol(symbol: str) -> dict:
    cfg = Config()
    bt = Backtester(cfg)
    r = bt.run(symbol, START, END, timeframe="1h", initial_balance=INITIAL_BALANCE)
    if not r.trades:
        return {"symbol": symbol, "trades": 0, "wins": 0, "pnl": 0.0, "final": INITIAL_BALANCE}
    wins = [t for t in r.trades if t.won]
    pnl = r.final - r.initial
    return {
        "symbol": symbol,
        "trades": len(r.trades),
        "wins": len(wins),
        "losses": len(r.trades) - len(wins),
        "pnl": pnl,
        "final": r.final,
        "pnl_pct": (r.final / r.initial - 1) * 100,
    }


def main():
    print(f"\n🔬 v87.0主張「10銘柄×1ヶ月+6.9%」検証")
    print(f"期間: {START} 〜 {END}")
    print(f"銘柄数: {len(SYMBOLS)}")
    print(f"各銘柄初期資金: ${INITIAL_BALANCE:,.0f}")
    print(f"{'='*70}\n")

    print(f"  {'銘柄':<13s} {'取引':>5s} {'勝敗':>8s} {'リターン':>10s} {'PnL':>12s}")
    print(f"  {'-'*66}")
    results = []
    for sym in SYMBOLS:
        r = run_symbol(sym)
        results.append(r)
        if r["trades"] == 0:
            print(f"  {sym:<13s} {r['trades']:>5d} {'-':>8s} {'-':>10s} {'-':>12s}")
        else:
            print(f"  {sym:<13s} {r['trades']:>5d} {r['wins']}W/{r['losses']}L  "
                  f"{r['pnl_pct']:+8.2f}% {r['pnl']:>+11,.0f}")

    # 集計
    total_trades = sum(r["trades"] for r in results)
    total_wins = sum(r["wins"] for r in results)
    total_pnl = sum(r["pnl"] for r in results)
    total_initial = INITIAL_BALANCE * len(SYMBOLS)
    portfolio_return = total_pnl / total_initial * 100
    win_rate = total_wins / total_trades * 100 if total_trades else 0

    print(f"\n{'='*70}")
    print(f"  📊 検証結果（現コードv95.0で同条件再現）")
    print(f"{'='*70}")
    print(f"  総取引数              : {total_trades}")
    print(f"  勝率                  : {win_rate:.1f}%")
    print(f"  総P&L                 : ${total_pnl:+,.2f}")
    print(f"  ポートフォリオ月利    : {portfolio_return:+.2f}%")
    print(f"{'='*70}")
    print(f"\n  📋 元の主張との比較")
    print(f"{'='*70}")
    print(f"  {'指標':<20s} {'主張(v87.0時)':>15s} {'現検証(v95.0)':>15s}")
    print(f"  {'-'*60}")
    print(f"  {'総取引数':<20s} {'16':>15s} {total_trades:>15d}")
    print(f"  {'勝率':<20s} {'37.5%':>15s} {f'{win_rate:.1f}%':>15s}")
    print(f"  {'総P&L':<20s} {'+$6,871':>15s} {f'${total_pnl:+,.0f}':>15s}")
    print(f"  {'月利':<20s} {'+6.9%':>15s} {f'{portfolio_return:+.2f}%':>15s}")
    print(f"{'='*70}")

    # 判定
    print(f"\n  🎯 判定:")
    if abs(portfolio_return - 6.9) <= 5:
        print(f"    ✅ 主張と近い結果。+6.9%の再現性あり。")
    elif portfolio_return > 0:
        print(f"    ⚠️ プラスだが主張より低い。部分的に再現。")
    else:
        print(f"    ❌ マイナスで、+6.9%の主張は再現不可能。")
    print(f"    ※ 注意: v87.0とv95.0でパラメータが大きく変わっているため")
    print(f"      完全な再現は不可能。これは現在のコードで同条件を試した結果。\n")


if __name__ == "__main__":
    main()
