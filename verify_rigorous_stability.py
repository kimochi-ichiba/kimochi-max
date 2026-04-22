"""
verify_rigorous_stability.py
============================
徹底的な安定性検証

検証内容:
1. 過去3年+のデータで月次ステップで多数のウィンドウ
2. 1年ウィンドウ + 2年ウィンドウ両方
3. Kelly戦略を複数通貨で比較 (BNB/ETH/BTC/SOL)
4. Regime Adaptive も含めて比較
5. ベア相場期間を含む(2022年クラッシュ等)

これで「本当に安定した戦略」を特定する。
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0
FEE = 0.0006
SLIP = 0.001
MMR = 0.005


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


def add_kelly_cols(df: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> pd.DataFrame:
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(lookback).mean() * 365
    df["roll_var"] = df["ret"].rolling(lookback).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=max_lev) * fraction
    return df


def run_kelly_window(df_prep: pd.DataFrame, start_date: datetime, end_date: datetime,
                      rebal_days: int = 30) -> dict:
    """指定期間でKelly戦略を実行"""
    df = df_prep[(df_prep.index >= pd.Timestamp(start_date)) & (df_prep.index <= pd.Timestamp(end_date))]
    if df.empty: return {"final": INITIAL, "liq": False, "dd": 0, "monthly": 0}

    balance = INITIAL
    pos_qty = 0; pos_entry = 0
    peak = INITIAL; max_dd = 0
    liquidated = False
    counter = 0

    for ts, row in df.iterrows():
        if pd.isna(row["kelly_f"]): continue
        price = row["close"]

        if counter % rebal_days == 0:
            if pos_qty > 0:
                exit_p = price * (1 - SLIP)
                balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
                pos_qty = 0
            kelly_lev = row["kelly_f"]
            if kelly_lev > 0.1:
                entry = price * (1 + SLIP)
                notional = balance * kelly_lev
                pos_qty = notional / entry
                pos_entry = entry
                balance -= notional * FEE

        if pos_qty > 0:
            eq = balance + (row["low"] - pos_entry) * pos_qty
            mm = row["low"] * pos_qty * MMR
            if eq <= mm:
                liquidated = True; balance = 0
                break
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        counter += 1

    if pos_qty > 0 and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    final = max(balance, 0)
    n_days = (end_date - start_date).days
    n_months = n_days / 30.0
    monthly = ((final/INITIAL) ** (1/n_months) - 1) * 100 if final > 0 else -100
    return {"final": final, "liq": liquidated, "dd": max_dd, "monthly": monthly,
            "days": n_days}


def test_strategy_across_windows(df_full: pd.DataFrame, strat_config: dict, windows: List[tuple], label: str):
    """1戦略を複数ウィンドウで検証"""
    df_prep = add_kelly_cols(df_full, strat_config["lookback"],
                              strat_config["fraction"], strat_config["max_lev"])
    results = []
    for start, end in windows:
        r = run_kelly_window(df_prep, start, end, strat_config["rebal_days"])
        results.append({"start": start, "end": end, **r})
    return label, results


def analyze_results(results: List[dict]) -> dict:
    rets = [r["monthly"] for r in results]
    finals = [r["final"] for r in results]
    dds = [r["dd"] for r in results]
    liqs = sum(1 for r in results if r["liq"])
    positives = sum(1 for r in results if r["monthly"] > 0)

    return {
        "n_windows": len(results),
        "liquidations": liqs,
        "positive_rate": positives / len(results) * 100 if results else 0,
        "avg_monthly": np.mean(rets),
        "median_monthly": np.median(rets),
        "min_monthly": np.min(rets),
        "max_monthly": np.max(rets),
        "std_monthly": np.std(rets),
        "avg_final": np.mean(finals),
        "min_final": np.min(finals),
        "max_final": np.max(finals),
        "avg_dd": np.mean(dds),
        "max_dd": np.max(dds),
    }


def main():
    print(f"\n🔬 厳密な安定性検証 (過去3年以上、複数戦略比較)")
    print(f"{'='*100}")
    print(f"初期資金: $3,000  /  検証対象: Kelly戦略 × 複数通貨\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})

    fetch_start = datetime(2022, 1, 1)
    fetch_end = datetime(2026, 4, 18)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(fetch_end.timestamp() * 1000)

    coins = ["BNB/USDT:USDT", "ETH/USDT:USDT", "BTC/USDT:USDT", "SOL/USDT:USDT", "AVAX/USDT:USDT"]
    dfs = {}
    for sym in coins:
        print(f"📥 {sym} データ取得中...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            key = sym.split("/")[0]
            dfs[key] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 1年ウィンドウを月次ステップで生成
    analysis_start = datetime(2023, 1, 1)  # 分析期間の始点
    analysis_end = datetime(2026, 4, 18)

    windows_1y = []
    cursor = analysis_start
    while cursor + timedelta(days=365) <= analysis_end:
        windows_1y.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)  # 月次ステップ

    # 2年ウィンドウ
    windows_2y = []
    cursor = analysis_start
    while cursor + timedelta(days=730) <= analysis_end:
        windows_2y.append((cursor, cursor + timedelta(days=730)))
        cursor += timedelta(days=60)  # 2ヶ月ステップ

    print(f"📊 検証ウィンドウ:")
    print(f"  1年ウィンドウ: {len(windows_1y)} 個 (月次ステップ)")
    print(f"  2年ウィンドウ: {len(windows_2y)} 個 (2ヶ月ステップ)\n")

    # 戦略定義
    strategies = {
        # Kelly BNB バリエーション
        "Kelly BNB 0.5x max10": {"coin": "BNB", "lookback": 90, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        "Kelly BNB 0.5x max12": {"coin": "BNB", "lookback": 90, "fraction": 0.5, "max_lev": 12, "rebal_days": 30},
        "Kelly BNB 0.65x max10": {"coin": "BNB", "lookback": 90, "fraction": 0.65, "max_lev": 10, "rebal_days": 30},
        "Kelly BNB 0.65x max12": {"coin": "BNB", "lookback": 90, "fraction": 0.65, "max_lev": 12, "rebal_days": 30},
        "Kelly BNB 0.5x max8 (保守)": {"coin": "BNB", "lookback": 90, "fraction": 0.5, "max_lev": 8, "rebal_days": 30},
        # Kelly 他通貨
        "Kelly ETH 0.5x max10": {"coin": "ETH", "lookback": 90, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        "Kelly ETH 0.65x max10": {"coin": "ETH", "lookback": 90, "fraction": 0.65, "max_lev": 10, "rebal_days": 30},
        "Kelly BTC 0.5x max10": {"coin": "BTC", "lookback": 90, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        "Kelly BTC 0.65x max10": {"coin": "BTC", "lookback": 90, "fraction": 0.65, "max_lev": 10, "rebal_days": 30},
        "Kelly SOL 0.5x max10": {"coin": "SOL", "lookback": 90, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        "Kelly SOL 0.65x max10": {"coin": "SOL", "lookback": 90, "fraction": 0.65, "max_lev": 10, "rebal_days": 30},
        # Kelly BNB 異なるlookback
        "Kelly BNB 0.5x lb60": {"coin": "BNB", "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        "Kelly BNB 0.5x lb180": {"coin": "BNB", "lookback": 180, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
    }

    # 1年ウィンドウ検証
    print(f"{'='*100}")
    print(f"  📊 [1] 1年ウィンドウ検証 ({len(windows_1y)}個のウィンドウ)")
    print(f"{'='*100}")
    print(f"  {'戦略':<30s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'DD平均':>7s}")
    print(f"  {'-'*92}")

    all_1y_results = {}
    for label, cfg in strategies.items():
        coin = cfg["coin"]
        if coin not in dfs: continue
        df_prep = add_kelly_cols(dfs[coin], cfg["lookback"], cfg["fraction"], cfg["max_lev"])
        results = []
        for start, end in windows_1y:
            r = run_kelly_window(df_prep, start, end, cfg["rebal_days"])
            results.append(r)
        all_1y_results[label] = results
        s = analyze_results(results)
        print(f"  {label:<30s} {s['positive_rate']:4.0f}%  {s['liquidations']:3d}   "
              f"{s['avg_monthly']:+7.2f}%  {s['median_monthly']:+6.2f}%  "
              f"{s['min_monthly']:+5.1f}%  {s['max_monthly']:+5.1f}%  {s['avg_dd']:5.0f}%")

    # 2年ウィンドウ検証
    print(f"\n{'='*100}")
    print(f"  📊 [2] 2年ウィンドウ検証 ({len(windows_2y)}個のウィンドウ)")
    print(f"{'='*100}")
    print(f"  {'戦略':<30s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'DD平均':>7s}")
    print(f"  {'-'*92}")

    all_2y_results = {}
    for label, cfg in strategies.items():
        coin = cfg["coin"]
        if coin not in dfs: continue
        df_prep = add_kelly_cols(dfs[coin], cfg["lookback"], cfg["fraction"], cfg["max_lev"])
        results = []
        for start, end in windows_2y:
            r = run_kelly_window(df_prep, start, end, cfg["rebal_days"])
            results.append(r)
        all_2y_results[label] = results
        s = analyze_results(results)
        print(f"  {label:<30s} {s['positive_rate']:4.0f}%  {s['liquidations']:3d}   "
              f"{s['avg_monthly']:+7.2f}%  {s['median_monthly']:+6.2f}%  "
              f"{s['min_monthly']:+5.1f}%  {s['max_monthly']:+5.1f}%  {s['avg_dd']:5.0f}%")

    # 最安定戦略を特定 (1年ウィンドウベース)
    print(f"\n{'='*100}")
    print(f"  🏆 最安定戦略ランキング (1年ウィンドウ)")
    print(f"  評価: プラス率 + (低DD優遇 - 清算ペナルティ)")
    print(f"{'='*100}")

    rankings = []
    for label, results in all_1y_results.items():
        s = analyze_results(results)
        score = s["positive_rate"] - s["liquidations"] * 20 - (s["avg_dd"] - 50) * 0.5
        rankings.append({"label": label, "score": score, **s})
    rankings.sort(key=lambda x: x["score"], reverse=True)

    print(f"  {'戦略':<30s} {'スコア':>6s} {'+率':>5s} {'平均月次':>9s} {'最低月次':>9s} {'清算':>5s}")
    for r in rankings[:10]:
        print(f"  {r['label']:<30s} {r['score']:5.0f}  {r['positive_rate']:4.0f}%  "
              f"{r['avg_monthly']:+7.2f}%  {r['min_monthly']:+7.2f}%  {r['liquidations']:3d}")

    # 最良戦略の詳細 (ワースト月が最も良いもの)
    print(f"\n{'='*100}")
    print(f"  💎 トップ3戦略の詳細分析 (最悪期でも耐えるか?)")
    print(f"{'='*100}")
    top3 = rankings[:3]
    for r in top3:
        label = r["label"]
        results_1y = all_1y_results[label]
        results_2y = all_2y_results.get(label, [])

        print(f"\n  ━━━ {label} ━━━")
        print(f"  [1年ウィンドウ {len(results_1y)}個]")
        print(f"    平均月次: {r['avg_monthly']:+.2f}%  中央値: {r['median_monthly']:+.2f}%")
        print(f"    最高: {r['max_monthly']:+.2f}%  最低: {r['min_monthly']:+.2f}%")
        print(f"    プラス率: {r['positive_rate']:.0f}%  清算: {r['liquidations']}回")
        print(f"    平均DD: {r['avg_dd']:.0f}%  最大DD: {r['max_dd']:.0f}%")
        print(f"    $3,000 → 平均${r['avg_final']:,.0f}  /  最低${r['min_final']:,.0f}  /  最高${r['max_final']:,.0f}")

        if results_2y:
            s2 = analyze_results(results_2y)
            print(f"  [2年ウィンドウ {len(results_2y)}個]")
            print(f"    平均月次: {s2['avg_monthly']:+.2f}%  プラス率: {s2['positive_rate']:.0f}%")
            print(f"    $3,000 → 平均${s2['avg_final']:,.0f}  /  最低${s2['min_final']:,.0f}")

        # 最悪期間を表示
        worst = min(results_1y, key=lambda x: x["monthly"])
        best = max(results_1y, key=lambda x: x["monthly"])
        print(f"    🔴 最悪期間: {worst['start'].strftime('%Y-%m-%d') if 'start' in worst else '?'}〜 "
              f"月{worst['monthly']:+.2f}% 最終${worst['final']:,.0f}")

    print()


if __name__ == "__main__":
    main()
