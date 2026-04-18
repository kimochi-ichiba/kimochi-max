"""
verify_ultimate_stability.py
============================
ハルシネーション疑惑を排除する最終検証

対象期間: 2022年1月 〜 2026年4月 (4年以上、2022クラッシュ含む)
検証方法:
  - 多数のウィンドウを月次ステップで生成 (33個以上)
  - 2年ウィンドウも同時検証 (21個以上)
  - Kelly 0.5x lb60 max10 を全通貨で試す
  - 複数通貨の均等分散も試す
  - 全ウィンドウの「生の結果」を出力 (隠蔽なし)

もしハルシネーションなら、多期間で必ず破綻する。
"""

from __future__ import annotations

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
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

# Kelly 最優秀設定
KELLY_CONFIG = {"lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30}


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


def run_window(df_prep: pd.DataFrame, start_date: datetime, end_date: datetime,
                rebal_days: int = 30, initial: float = INITIAL) -> dict:
    df = df_prep[(df_prep.index >= pd.Timestamp(start_date)) & (df_prep.index <= pd.Timestamp(end_date))]
    if df.empty or len(df) < 30:
        return {"final": initial, "liq": False, "dd": 0, "monthly": 0, "skip": True}

    balance = initial
    pos_qty = 0; pos_entry = 0
    peak = initial; max_dd = 0
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
    monthly = ((final/initial) ** (1/n_months) - 1) * 100 if final > 0 else -100
    return {"final": final, "liq": liquidated, "dd": max_dd, "monthly": monthly,
            "skip": False, "days": n_days}


def run_multi_coin_window(dfs_prep: Dict[str, pd.DataFrame], weights: Dict[str, float],
                           start_date: datetime, end_date: datetime, rebal_days: int = 30) -> dict:
    """複数通貨分散での運用"""
    total_final = 0
    worst_dd = 0
    any_liq = False
    skipped = True
    for sym, w in weights.items():
        if sym not in dfs_prep: continue
        if w <= 0: continue
        init_alloc = INITIAL * w
        r = run_window(dfs_prep[sym], start_date, end_date, rebal_days, init_alloc)
        if r.get("skip"): continue
        skipped = False
        total_final += r["final"]
        worst_dd = max(worst_dd, r["dd"])
        if r["liq"]: any_liq = True

    if skipped: return {"final": INITIAL, "liq": False, "dd": 0, "monthly": 0, "skip": True}
    n_days = (end_date - start_date).days
    n_months = n_days / 30.0
    monthly = ((total_final/INITIAL) ** (1/n_months) - 1) * 100 if total_final > 0 else -100
    return {"final": total_final, "liq": any_liq, "dd": worst_dd,
            "monthly": monthly, "skip": False, "days": n_days}


def analyze(results: List[dict]) -> dict:
    valid = [r for r in results if not r.get("skip", False)]
    if not valid:
        return {"n": 0, "pos_rate": 0, "avg": 0, "median": 0, "min": 0, "max": 0,
                "liqs": 0, "avg_final": 0, "min_final": 0, "max_final": 0}
    rets = [r["monthly"] for r in valid]
    finals = [r["final"] for r in valid]
    return {
        "n": len(valid),
        "pos_rate": sum(1 for r in rets if r > 0) / len(rets) * 100,
        "avg": np.mean(rets),
        "median": np.median(rets),
        "min": np.min(rets),
        "max": np.max(rets),
        "std": np.std(rets),
        "liqs": sum(1 for r in valid if r["liq"]),
        "avg_final": np.mean(finals),
        "min_final": np.min(finals),
        "max_final": np.max(finals),
        "avg_dd": np.mean([r["dd"] for r in valid]),
    }


def main():
    print(f"\n🔬 ハルシネーション疑惑排除・究極安定性検証")
    print(f"{'='*105}")
    print(f"対象期間: 2022-01 〜 2026-04 (4年以上・2022クラッシュ含む)")
    print(f"初期資金: ${INITIAL:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    fetch_start = datetime(2021, 6, 1)
    fetch_end = datetime(2026, 4, 18)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(fetch_end.timestamp() * 1000)

    # 多数の通貨
    coins = {
        "BNB": "BNB/USDT:USDT", "ETH": "ETH/USDT:USDT", "BTC": "BTC/USDT:USDT",
        "SOL": "SOL/USDT:USDT", "AVAX": "AVAX/USDT:USDT", "LINK": "LINK/USDT:USDT",
        "ADA": "ADA/USDT:USDT", "DOT": "DOT/USDT:USDT", "XRP": "XRP/USDT:USDT",
        "DOGE": "DOGE/USDT:USDT", "LTC": "LTC/USDT:USDT", "BCH": "BCH/USDT:USDT",
        "MATIC": "MATIC/USDT:USDT",
    }

    dfs = {}
    for name, sym in coins.items():
        print(f"📥 {name}...", end="")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty and len(df) >= 200:
            dfs[name] = df
            print(f" ✅ {len(df)}本 ({df.index[0].strftime('%Y-%m-%d')}〜)")
        else:
            print(f" ⚠️ データ不足")
    print()

    # Kelly 列準備
    dfs_prep = {}
    for name, df in dfs.items():
        dfs_prep[name] = add_kelly_cols(df, KELLY_CONFIG["lookback"],
                                          KELLY_CONFIG["fraction"], KELLY_CONFIG["max_lev"])

    # ウィンドウ生成: 分析開始2022-06から、2022クラッシュ含む
    analysis_start = datetime(2022, 6, 1)
    analysis_end = datetime(2026, 4, 18)

    windows_1y = []
    cursor = analysis_start
    while cursor + timedelta(days=365) <= analysis_end:
        windows_1y.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)

    windows_2y = []
    cursor = analysis_start
    while cursor + timedelta(days=730) <= analysis_end:
        windows_2y.append((cursor, cursor + timedelta(days=730)))
        cursor += timedelta(days=60)

    print(f"📊 検証ウィンドウ:")
    print(f"  1年ウィンドウ: {len(windows_1y)}個 (月次ステップ)")
    print(f"  2年ウィンドウ: {len(windows_2y)}個 (2ヶ月ステップ)\n")

    # === 単一通貨 Kelly 検証 ===
    print(f"{'='*105}")
    print(f"  📊 [1] 単一通貨 Kelly 0.5x lb60 max10 @ 30d rebal - 1年ウィンドウ")
    print(f"{'='*105}")
    print(f"  {'通貨':<6s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'平均$':>10s} {'最低$':>9s}")
    print(f"  {'-'*90}")

    single_results_1y = {}
    for name in sorted(dfs_prep.keys()):
        results = [run_window(dfs_prep[name], s, e, KELLY_CONFIG["rebal_days"]) for s, e in windows_1y]
        single_results_1y[name] = results
        s = analyze(results)
        if s["n"] == 0: continue
        print(f"  {name:<6s} {s['pos_rate']:4.0f}%  {s['liqs']:3d}   "
              f"{s['avg']:+7.2f}%  {s['median']:+6.2f}%  {s['min']:+5.1f}%  {s['max']:+5.1f}%  "
              f"${s['avg_final']:>7,.0f}  ${s['min_final']:>7,.0f}")

    # 2年ウィンドウ
    print(f"\n{'='*105}")
    print(f"  📊 [2] 単一通貨 Kelly - 2年ウィンドウ")
    print(f"{'='*105}")
    print(f"  {'通貨':<6s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'平均$':>10s} {'最低$':>9s}")
    print(f"  {'-'*90}")

    single_results_2y = {}
    for name in sorted(dfs_prep.keys()):
        results = [run_window(dfs_prep[name], s, e, KELLY_CONFIG["rebal_days"]) for s, e in windows_2y]
        single_results_2y[name] = results
        s = analyze(results)
        if s["n"] == 0: continue
        print(f"  {name:<6s} {s['pos_rate']:4.0f}%  {s['liqs']:3d}   "
              f"{s['avg']:+7.2f}%  {s['median']:+6.2f}%  {s['min']:+5.1f}%  {s['max']:+5.1f}%  "
              f"${s['avg_final']:>7,.0f}  ${s['min_final']:>7,.0f}")

    # === 複数通貨分散 ===
    print(f"\n{'='*105}")
    print(f"  📊 [3] 複数通貨分散Kelly戦略 - 1年ウィンドウ")
    print(f"{'='*105}")

    # 分散パターン
    diverse_combos = [
        {"name": "BNB+ETH 50/50", "weights": {"BNB": 0.5, "ETH": 0.5}},
        {"name": "BNB+BTC 50/50", "weights": {"BNB": 0.5, "BTC": 0.5}},
        {"name": "BNB+ETH+BTC 均等", "weights": {"BNB": 0.33, "ETH": 0.34, "BTC": 0.33}},
        {"name": "BNB 70 + BTC 30", "weights": {"BNB": 0.7, "BTC": 0.3}},
        {"name": "BNB 60 + ETH 20 + BTC 20", "weights": {"BNB": 0.6, "ETH": 0.2, "BTC": 0.2}},
        {"name": "5通貨均等 (BNB/ETH/BTC/LINK/ADA)",
         "weights": {"BNB": 0.2, "ETH": 0.2, "BTC": 0.2, "LINK": 0.2, "ADA": 0.2}},
    ]

    print(f"  {'戦略':<40s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'平均$':>10s}")
    print(f"  {'-'*95}")
    for combo in diverse_combos:
        results = [run_multi_coin_window(dfs_prep, combo["weights"], s, e) for s, e in windows_1y]
        s = analyze(results)
        if s["n"] == 0: continue
        print(f"  {combo['name']:<40s} {s['pos_rate']:4.0f}%  {s['liqs']:3d}   "
              f"{s['avg']:+7.2f}%  {s['median']:+6.2f}%  {s['min']:+5.1f}%  {s['max']:+5.1f}%  "
              f"${s['avg_final']:>7,.0f}")

    # 2年
    print(f"\n{'='*105}")
    print(f"  📊 [4] 複数通貨分散Kelly戦略 - 2年ウィンドウ")
    print(f"{'='*105}")
    print(f"  {'戦略':<40s} {'+率':>5s} {'清算':>5s} {'平均月次':>9s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'平均$':>10s}")
    print(f"  {'-'*95}")
    for combo in diverse_combos:
        results = [run_multi_coin_window(dfs_prep, combo["weights"], s, e) for s, e in windows_2y]
        s = analyze(results)
        if s["n"] == 0: continue
        print(f"  {combo['name']:<40s} {s['pos_rate']:4.0f}%  {s['liqs']:3d}   "
              f"{s['avg']:+7.2f}%  {s['median']:+6.2f}%  {s['min']:+5.1f}%  {s['max']:+5.1f}%  "
              f"${s['avg_final']:>7,.0f}")

    # === 最悪期の詳細 (BNBの個別期間) ===
    print(f"\n{'='*105}")
    print(f"  📋 BNB単独 Kelly 全ウィンドウ詳細 (隠蔽なし)")
    print(f"{'='*105}")
    if "BNB" in single_results_1y:
        bnb_results = single_results_1y["BNB"]
        print(f"  {'#':>3s} {'期間':<30s} {'最終$':>10s} {'月次':>8s} {'DD':>6s} {'状態':>8s}")
        print(f"  {'-'*75}")
        for i, (r, (s, e)) in enumerate(zip(bnb_results, windows_1y), 1):
            status = "💀清算" if r.get("liq") else "✓生存"
            final_str = f"${r['final']:,.0f}"
            print(f"  {i:>3d} {s.strftime('%Y-%m-%d')}〜{e.strftime('%Y-%m-%d')}  "
                  f"{final_str:>10s}  {r['monthly']:+6.2f}%  {r['dd']:5.0f}%  {status:>8s}")

    # === 最終判定 ===
    print(f"\n{'='*105}")
    print(f"  🎯 最終判定: ハルシネーションか本物か")
    print(f"{'='*105}")

    # 各戦略のスコア比較
    all_evaluations = []
    for name, results in single_results_1y.items():
        s = analyze(results)
        if s["n"] == 0: continue
        all_evaluations.append({
            "type": "single",
            "name": f"Kelly {name}",
            **s
        })

    for combo in diverse_combos:
        results = [run_multi_coin_window(dfs_prep, combo["weights"], s, e) for s, e in windows_1y]
        s = analyze(results)
        if s["n"] == 0: continue
        all_evaluations.append({
            "type": "multi",
            "name": combo["name"],
            **s
        })

    # 安定性ランキング (プラス率 + 平均リターン)
    all_evaluations.sort(key=lambda x: (x["pos_rate"], x["avg"]), reverse=True)
    print(f"  {'戦略':<40s} {'+率':>5s} {'清算':>5s} {'月次平均':>9s} {'最低月次':>9s} {'平均$':>10s}")
    print(f"  {'-'*88}")
    for e in all_evaluations[:10]:
        print(f"  {e['name']:<40s} {e['pos_rate']:4.0f}%  {e['liqs']:3d}   "
              f"{e['avg']:+7.2f}%  {e['min']:+7.2f}%  ${e['avg_final']:>7,.0f}")

    # 本物かの判定
    best = all_evaluations[0] if all_evaluations else None
    print(f"\n{'='*105}")
    if best and best["pos_rate"] >= 85 and best["liqs"] == 0 and best["avg"] >= 5:
        print(f"  ✅ 「{best['name']}」は本物の安定戦略!")
        print(f"     プラス率{best['pos_rate']:.0f}%、清算{best['liqs']}回、月次+{best['avg']:.2f}%")
    elif best and best["pos_rate"] >= 70 and best["liqs"] == 0:
        print(f"  🎯 「{best['name']}」は一定の安定性あり")
        print(f"     プラス率{best['pos_rate']:.0f}%、清算{best['liqs']}回、月次+{best['avg']:.2f}%")
    else:
        print(f"  ⚠️ 完全安定戦略は特定できず、ハルシネーションの疑いあり")
    print()


if __name__ == "__main__":
    main()
