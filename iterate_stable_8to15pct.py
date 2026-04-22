"""
iterate_stable_8to15pct.py
==========================
$3,000スタート、月+8〜15%の安定戦略を反復検証

観点:
1. ETHに固執せず多通貨組み合わせ
2. 1ヶ月の勝率 (23ウィンドウ統計)
3. 1年複利リターン
4. DDも重視 (ハイリターンだけでなく安定性)
5. 清算なし

検証対象:
- BNB+ETH / BNB+AVAX / BNB+LINK等 2通貨
- ETH+BNB+AVAX / ETH+BNB+LINK等 3通貨
- 上位モメンタム動的切替
- Market regime filtered (ベア回避)
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0   # ユーザー希望
END_DATE = datetime(2026, 4, 18)
YEAR_DAYS = 365
FETCH_START = END_DATE - timedelta(days=YEAR_DAYS + 200)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

COINS = {
    "BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "BNB": "BNB/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT", "LINK": "LINK/USDT:USDT", "SOL": "SOL/USDT:USDT",
    "XRP": "XRP/USDT:USDT", "ADA": "ADA/USDT:USDT",
}


def fetch_ohlcv(ex, symbol, since_ms, until_ms):
    tf_ms = 86400 * 1000
    all_data = []
    current = since_ms
    while current < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, "1d", since=current, limit=1000)
            if not batch: break
            batch = [c for c in batch if c[0] < until_ms]
            all_data.extend(batch)
            if len(batch) < 1000: break
            current = batch[-1][0] + tf_ms
            time.sleep(0.1)
        except Exception:
            break
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").drop_duplicates().sort_index().astype(float)


def sim_hold_period(df, alloc_cash, leverage, start_ts, end_ts):
    """指定期間だけホールドしてPnL計算"""
    period_df = df[(df.index >= start_ts) & (df.index <= end_ts)]
    if period_df.empty or len(period_df) < 2:
        return alloc_cash, False, 0
    entry = period_df["close"].iloc[0] * (1 + SLIP)
    notional = alloc_cash * leverage
    qty = notional / entry
    cash = alloc_cash - notional * FEE
    peak = alloc_cash
    max_dd = 0
    for p in period_df["low"]:
        current_equity = cash + (p - entry) * qty
        mm = p * qty * MMR
        if current_equity <= mm:
            return 0, True, 100
        if current_equity > peak: peak = current_equity
        if peak > 0:
            dd = (peak - current_equity) / peak * 100
            max_dd = max(max_dd, dd)
    exit_p = period_df["close"].iloc[-1] * (1 - SLIP)
    cash += (exit_p - entry) * qty - exit_p * qty * FEE
    return cash, False, max_dd


def portfolio_period(dfs, allocs, leverage, start_ts, end_ts):
    """ポートフォリオで期間ホールド"""
    total_final = 0
    any_liq = False
    worst_dd = 0
    for sym, w in allocs.items():
        if sym not in dfs or w <= 0: continue
        alloc = INITIAL * w
        fv, liq, dd = sim_hold_period(dfs[sym], alloc, leverage, start_ts, end_ts)
        total_final += fv
        worst_dd = max(worst_dd, dd)
        if liq: any_liq = True
    return total_final, any_liq, worst_dd


def test_strategy(dfs, allocs, leverage, label, windows):
    """
    1年連続運用と、複数1ヶ月ウィンドウでの統計を両方返す
    """
    # 1) 1年通し (Buy&Hold)
    annual_ts = pd.Timestamp(END_DATE - timedelta(days=YEAR_DAYS))
    end_ts = pd.Timestamp(END_DATE)
    annual_final, annual_liq, annual_dd = portfolio_period(dfs, allocs, leverage, annual_ts, end_ts)
    annual_ret = (annual_final/INITIAL - 1) * 100
    annual_monthly = ((annual_final/INITIAL) ** (1/12) - 1) * 100 if annual_final > 0 else -100

    # 2) 1ヶ月ウィンドウの分布
    monthly_rets = []
    monthly_dd = []
    monthly_liqs = 0
    for ws, we in windows:
        m_final, m_liq, m_dd = portfolio_period(dfs, allocs, leverage, ws, we)
        monthly_rets.append((m_final/INITIAL - 1) * 100)
        monthly_dd.append(m_dd)
        if m_liq: monthly_liqs += 1

    rets = np.array(monthly_rets)
    return {
        "name": label,
        "annual_final": annual_final,
        "annual_return": annual_ret,
        "annual_monthly": annual_monthly,
        "annual_dd": annual_dd,
        "annual_liquidated": annual_liq,
        "monthly_mean": np.mean(rets),
        "monthly_median": np.median(rets),
        "monthly_max": np.max(rets),
        "monthly_min": np.min(rets),
        "monthly_std": np.std(rets),
        "positive_months": sum(1 for r in rets if r > 0),
        "months_8to15": sum(1 for r in rets if 8 <= r <= 15),
        "months_over_15": sum(1 for r in rets if r > 15),
        "months_liquidated": monthly_liqs,
        "total_windows": len(rets),
        "allocs": allocs,
        "leverage": leverage,
    }


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🎯 $3,000スタート・安定月+8〜15%の反復検証")
    print(f"{'='*95}")
    print(f"期間: 1年 ({(END_DATE - timedelta(days=YEAR_DAYS)).strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')})")
    print(f"初期資金: ${INITIAL:,.0f}")
    print(f"{'='*95}\n")

    print(f"📥 データ取得中...")
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    dfs = {}
    for name, sym in COINS.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 各通貨の1年パフォーマンス
    print(f"📊 過去1年の各通貨Buy&Holdリターン:")
    for name, df in dfs.items():
        d = df[df.index >= pd.Timestamp(END_DATE - timedelta(days=YEAR_DAYS))]
        if d.empty: continue
        r = (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100
        print(f"    {name:<5s}: {r:+7.2f}%")
    print()

    # 1ヶ月ウィンドウの生成 (15日刻み × 23ウィンドウ)
    start_base = END_DATE - timedelta(days=YEAR_DAYS)
    windows = []
    cursor = start_base
    while cursor + timedelta(days=30) <= END_DATE:
        windows.append((pd.Timestamp(cursor), pd.Timestamp(cursor + timedelta(days=30))))
        cursor += timedelta(days=15)
    print(f"📊 1ヶ月ウィンドウ数: {len(windows)}\n")

    # 戦略リスト
    tests = [
        # BNB単独
        ("BNB 100% @ 2x",       {"BNB": 1.0}, 2),
        ("BNB 100% @ 3x",       {"BNB": 1.0}, 3),
        ("BNB 100% @ 4x",       {"BNB": 1.0}, 4),
        # ETH単独
        ("ETH 100% @ 2x",       {"ETH": 1.0}, 2),
        ("ETH 100% @ 3x",       {"ETH": 1.0}, 3),
        ("ETH 100% @ 4x",       {"ETH": 1.0}, 4),
        # BNB + ETH 組合せ
        ("BNB70+ETH30 @ 3x",    {"BNB": 0.7, "ETH": 0.3}, 3),
        ("BNB60+ETH40 @ 3x",    {"BNB": 0.6, "ETH": 0.4}, 3),
        ("BNB50+ETH50 @ 3x",    {"BNB": 0.5, "ETH": 0.5}, 3),
        ("BNB50+ETH50 @ 4x",    {"BNB": 0.5, "ETH": 0.5}, 4),
        ("BNB60+ETH40 @ 4x",    {"BNB": 0.6, "ETH": 0.4}, 4),
        ("BNB70+ETH30 @ 4x",    {"BNB": 0.7, "ETH": 0.3}, 4),
        # BNB + 他
        ("BNB50+AVAX50 @ 3x",   {"BNB": 0.5, "AVAX": 0.5}, 3),
        ("BNB50+LINK50 @ 3x",   {"BNB": 0.5, "LINK": 0.5}, 3),
        # 3銘柄
        ("BNB50+ETH30+AVAX20 @ 3x", {"BNB": 0.5, "ETH": 0.3, "AVAX": 0.2}, 3),
        ("BNB40+ETH30+AVAX30 @ 3x", {"BNB": 0.4, "ETH": 0.3, "AVAX": 0.3}, 3),
        ("BNB40+ETH40+LINK20 @ 3x", {"BNB": 0.4, "ETH": 0.4, "LINK": 0.2}, 3),
        # 3銘柄4x
        ("BNB50+ETH30+AVAX20 @ 4x", {"BNB": 0.5, "ETH": 0.3, "AVAX": 0.2}, 4),
        ("BNB50+ETH30+LINK20 @ 4x", {"BNB": 0.5, "ETH": 0.3, "LINK": 0.2}, 4),
    ]

    results = []
    for label, allocs, leverage in tests:
        print(f"🔬 {label}")
        r = test_strategy(dfs, allocs, leverage, label, windows)
        results.append(r)
        status = "🎯" if 8 <= r["annual_monthly"] <= 20 else ("✅" if r["annual_monthly"] > 20 else "⚠️")
        liq_s = " 清算" if r["annual_liquidated"] else ""
        print(f"   {status} 1年: 月{r['annual_monthly']:+.2f}% 年{r['annual_return']:+.1f}% DD{r['annual_dd']:.0f}%{liq_s}  "
              f"月次中央値{r['monthly_median']:+.1f}%  +8〜15%月:{r['months_8to15']}/{r['total_windows']}")

    # ランキング: 安定性重視 (月次mean - std で risk-adjusted)
    print(f"\n{'='*95}")
    print(f"  📊 ランキング (月+8〜15%達成月数で並べ替え)")
    print(f"{'='*95}")
    results.sort(key=lambda x: (x["months_8to15"], x["annual_monthly"]), reverse=True)
    print(f"  {'戦略':<32s} {'月次平均':>9s} {'年率':>8s} {'DD':>6s} {'中央':>7s} {'+8-15%':>8s} {'最終':>10s}")
    print(f"  {'-'*92}")
    for r in results:
        final_str = f"${r['annual_final']:,.0f}" if not r['annual_liquidated'] else "💀清算"
        print(f"  {r['name']:<32s} {r['annual_monthly']:+7.2f}%  {r['annual_return']:+6.1f}%  "
              f"{r['annual_dd']:5.0f}%  {r['monthly_median']:+5.1f}%  "
              f"{r['months_8to15']}/{r['total_windows']:<3d}   {final_str:>10s}")

    # Best候補 (清算なしで安定性の高いもの)
    safe_stable = [r for r in results if not r["annual_liquidated"] and
                    r["months_liquidated"] == 0 and 8 <= r["annual_monthly"] <= 20]
    print(f"\n  🏆 推奨候補 (清算なし・月次+8〜20%に入るもの):")
    if safe_stable:
        safe_stable.sort(key=lambda x: x["months_8to15"], reverse=True)
        for r in safe_stable[:5]:
            print(f"\n  ━━━ {r['name']} ━━━")
            print(f"    📈 年間結果    : ${INITIAL:,.0f} → ${r['annual_final']:,.0f} ({r['annual_return']:+.1f}%)")
            print(f"    📊 月次複利    : {r['annual_monthly']:+.2f}%")
            print(f"    📉 最大DD      : {r['annual_dd']:.0f}%")
            print(f"    🎯 月別内訳:")
            print(f"        中央値     : {r['monthly_median']:+.1f}%")
            print(f"        最高       : {r['monthly_max']:+.1f}%  最低: {r['monthly_min']:+.1f}%")
            print(f"        +8〜15%の月: {r['months_8to15']}/{r['total_windows']}")
            print(f"        +15%超え月 : {r['months_over_15']}/{r['total_windows']}")
            print(f"        プラス月数 : {r['positive_months']}/{r['total_windows']}")
    else:
        print(f"    ⚠️ 月+8-15%を安定して出す戦略は見つからず")

    # 最終サマリー
    print(f"\n{'='*95}")
    print(f"  🎯 $3,000 → 最終資金シミュレーション (推奨戦略の年間結果)")
    print(f"{'='*95}")
    for r in (safe_stable[:3] if safe_stable else results[:3]):
        final = r['annual_final']
        profit = final - INITIAL
        multi = final / INITIAL
        print(f"  {r['name']:<35s}: $3,000 → ${final:,.0f}  (利益 +${profit:,.0f}, {multi:.2f}倍)")
    print()


if __name__ == "__main__":
    main()
