"""
kelly_bot_final_verify.py
=========================
修正済みkelly_bot.pyの計算ロジックを流用した最終確認バックテスト

目的: kelly_bot.py が正しく動くことを検証
方法: bot と同じ計算式を使ってバックテスト
期待値: 複数期間で安定した正のリターン
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

# kelly_bot.py と同じ設定
CONFIG = {
    "allocations": {"BNB": 0.70, "BTC": 0.30},
    "kelly_fraction": 0.5,
    "lookback_days": 60,
    "max_leverage": 10.0,
    "rebalance_days": 30,
    "min_leverage_threshold": 0.1,
    "fee_rate": 0.0006,
    "slippage": 0.001,
    "mmr": 0.005,
    "initial_capital": 3000.0,
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


def compute_kelly(df_hist, cfg):
    """kelly_bot.py と同じ実装"""
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < cfg["lookback_days"]: return 0.0
    recent = returns.tail(cfg["lookback_days"])
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * cfg["kelly_fraction"]
    return float(np.clip(kelly, 0, cfg["max_leverage"]))


@dataclass
class Pos:
    entry_price: float
    size: float  # レバ込みsize
    leverage: float
    margin: float


def run_bot_sim(dfs, cfg, start, end):
    """kelly_bot.py のロジックを再現したシミュレーション"""
    cash = cfg["initial_capital"]
    positions: Dict[str, Pos] = {}
    last_rebal = None
    peak = cfg["initial_capital"]
    max_dd = 0

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    for ts in all_dates:
        # 清算チェック (バグ修正: size*leverage ではなく size)
        to_remove = []
        for sym, pos in positions.items():
            if sym not in dfs or ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            current_pnl = (low - pos.entry_price) * pos.size  # ✅ 正しい
            eq = pos.margin + current_pnl
            mm = low * pos.size * cfg["mmr"]
            if eq <= mm:
                to_remove.append(sym)
        for sym in to_remove:
            del positions[sym]

        # リバランス
        if last_rebal is None or (ts - last_rebal).days >= cfg["rebalance_days"]:
            # 決済 (バグ修正)
            for sym, pos in list(positions.items()):
                if sym not in dfs or ts not in dfs[sym].index: continue
                price = dfs[sym].loc[ts]["close"]
                exit_p = price * (1 - cfg["slippage"])
                pnl = (exit_p - pos.entry_price) * pos.size  # ✅ 正しい
                fee = exit_p * pos.size * cfg["fee_rate"]
                final = max(pos.margin + pnl - fee, 0)
                cash += final
                del positions[sym]

            # エントリー
            total = cash
            for sym, weight in cfg["allocations"].items():
                if sym not in dfs or ts not in dfs[sym].index: continue
                df_hist = dfs[sym][dfs[sym].index < ts].tail(cfg["lookback_days"] + 30)
                kelly_lev = compute_kelly(df_hist, cfg)
                if kelly_lev < cfg["min_leverage_threshold"]: continue

                alloc = total * weight
                current = dfs[sym].loc[ts]["close"]
                entry = current * (1 + cfg["slippage"])
                notional = alloc * kelly_lev
                size = notional / entry
                fee = notional * cfg["fee_rate"]
                margin = alloc - fee

                positions[sym] = Pos(entry_price=entry, size=size, leverage=kelly_lev, margin=margin)
                cash -= margin

            last_rebal = ts

        # equity追跡
        eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                eq += pos.margin + (p - pos.entry_price) * pos.size  # ✅ 正しい
            else:
                eq += pos.margin

        if eq > peak: peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

    # 最終決済
    if all_dates and positions:
        ts = all_dates[-1]
        for sym, pos in list(positions.items()):
            if sym in dfs and ts in dfs[sym].index:
                price = dfs[sym].loc[ts]["close"]
                exit_p = price * (1 - cfg["slippage"])
                pnl = (exit_p - pos.entry_price) * pos.size
                fee = exit_p * pos.size * cfg["fee_rate"]
                cash += max(pos.margin + pnl - fee, 0)

    return {"final": cash, "max_dd": max_dd}


def main():
    print(f"\n✅ kelly_bot.py 最終動作確認バックテスト")
    print(f"{'='*95}")
    print(f"戦略: BNB 70% + BTC 30%  /  Kelly 0.5x lb60 max10x rebal30d")
    print(f"初期資金: ${CONFIG['initial_capital']:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        print(f"📥 {name}...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ 取得完了\n")

    # 多数の1年ウィンドウ
    start_base = datetime(2022, 6, 1)
    end_base = datetime(2026, 4, 18)
    windows_1y = []
    cursor = start_base
    while cursor + timedelta(days=365) <= end_base:
        windows_1y.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)

    windows_2y = []
    cursor = start_base
    while cursor + timedelta(days=730) <= end_base:
        windows_2y.append((cursor, cursor + timedelta(days=730)))
        cursor += timedelta(days=60)

    print(f"1年ウィンドウ: {len(windows_1y)}個  /  2年ウィンドウ: {len(windows_2y)}個\n")

    # 1年
    print(f"{'='*95}")
    print(f"  📊 1年ウィンドウ結果 (BNB70 + BTC30)")
    print(f"{'='*95}")
    rets_1y = []
    for start, end in windows_1y:
        r = run_bot_sim(dfs, CONFIG, start, end)
        final = r["final"]
        months = (end - start).days / 30.0
        monthly = ((final/CONFIG["initial_capital"])**(1/months)-1)*100 if final > 0 else -100
        rets_1y.append(monthly)

    positive_1y = sum(1 for m in rets_1y if m > 0)
    over10_1y = sum(1 for m in rets_1y if m >= 10)
    over8_1y = sum(1 for m in rets_1y if m >= 8)
    print(f"  プラス率: {positive_1y}/{len(rets_1y)} ({positive_1y/len(rets_1y)*100:.0f}%)")
    print(f"  平均月次: {np.mean(rets_1y):+.2f}%")
    print(f"  中央値: {np.median(rets_1y):+.2f}%")
    print(f"  最高 / 最低: {np.max(rets_1y):+.2f}% / {np.min(rets_1y):+.2f}%")
    print(f"  月+8%以上: {over8_1y}/{len(rets_1y)} ({over8_1y/len(rets_1y)*100:.0f}%)")
    print(f"  月+10%以上: {over10_1y}/{len(rets_1y)} ({over10_1y/len(rets_1y)*100:.0f}%)")

    # 2年
    print(f"\n{'='*95}")
    print(f"  📊 2年ウィンドウ結果 (BNB70 + BTC30)")
    print(f"{'='*95}")
    rets_2y = []
    for start, end in windows_2y:
        r = run_bot_sim(dfs, CONFIG, start, end)
        final = r["final"]
        months = (end - start).days / 30.0
        monthly = ((final/CONFIG["initial_capital"])**(1/months)-1)*100 if final > 0 else -100
        rets_2y.append(monthly)

    positive_2y = sum(1 for m in rets_2y if m > 0)
    over10_2y = sum(1 for m in rets_2y if m >= 10)
    over8_2y = sum(1 for m in rets_2y if m >= 8)
    print(f"  プラス率: {positive_2y}/{len(rets_2y)} ({positive_2y/len(rets_2y)*100:.0f}%)")
    print(f"  平均月次: {np.mean(rets_2y):+.2f}%")
    print(f"  中央値: {np.median(rets_2y):+.2f}%")
    print(f"  最高 / 最低: {np.max(rets_2y):+.2f}% / {np.min(rets_2y):+.2f}%")
    print(f"  月+8%以上: {over8_2y}/{len(rets_2y)}")
    print(f"  月+10%以上: {over10_2y}/{len(rets_2y)}")

    # 最終判定
    print(f"\n{'='*95}")
    print(f"  🎯 最終判定")
    print(f"{'='*95}")
    if positive_1y / len(rets_1y) >= 0.85 and np.mean(rets_1y) >= 6:
        print(f"  ✅✅ kelly_bot.py は本物の戦略!")
        print(f"  1年運用で{positive_1y/len(rets_1y)*100:.0f}%プラス、月次平均+{np.mean(rets_1y):.2f}%")
    elif positive_1y / len(rets_1y) >= 0.7:
        print(f"  ✅ 概ね安定 ({positive_1y/len(rets_1y)*100:.0f}%プラス、月次+{np.mean(rets_1y):.2f}%)")
    else:
        print(f"  ⚠️ 安定性低い (プラス率{positive_1y/len(rets_1y)*100:.0f}%)")

    # $3,000実績
    print(f"\n  💰 $3,000 実運用期待値 (2年運用)")
    if rets_2y:
        avg_m = np.mean(rets_2y)
        avg_final = 3000 * (1 + avg_m/100) ** 24
        print(f"    平均月次 +{avg_m:.2f}% で 2年運用 → 期待値 ${avg_final:,.0f}")
        print(f"    最悪期間: ${3000 * (1 + np.min(rets_2y)/100) ** 24:,.0f}")
        print(f"    最高期間: ${3000 * (1 + np.max(rets_2y)/100) ** 24:,.0f}")
    print()


if __name__ == "__main__":
    main()
