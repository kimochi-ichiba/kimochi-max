"""
compare_improvements.py
=======================
改善案の比較バックテスト

テストする改善:
1. Baseline: 現状 (min_threshold=0.1, no buffer)
2. V2: Kelly<1.0スキップ (min_threshold=1.0)
3. V3: V2 + 現金バッファ5%
4. V4: V3 + ポジション単位SL (-20%)
5. V5: V4 + DD-35%でCircuit Breaker
6. V6: V5 + 実トレード時の配分最適化 (notional基準)
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3000.0
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


def compute_kelly(df_hist, lookback, fraction, max_lev):
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    return float(np.clip(kelly, 0, max_lev))


@dataclass
class Pos:
    entry: float
    size: float
    lev: float
    margin: float
    high_water: float = 0


def run_improved_bot(dfs, cfg, start, end):
    """改善版ボット実装"""
    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL
    cooldown_count = 0
    liquidations = 0
    stop_loss_count = 0
    circuit_breaker_tripped = False

    peak_portfolio = INITIAL
    max_dd = 0

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    for ts in all_dates:
        # 現在の総資産計算
        current_equity = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                current_equity += pos.margin + (p - pos.entry) * pos.size

        # DD更新
        if current_equity > peak_portfolio: peak_portfolio = current_equity
        if peak_portfolio > 0:
            dd = (peak_portfolio - current_equity) / peak_portfolio * 100
            max_dd = max(max_dd, dd)

        # Circuit Breaker判定
        if cfg.get("circuit_breaker_dd") and dd >= cfg["circuit_breaker_dd"]:
            if not circuit_breaker_tripped:
                circuit_breaker_tripped = True
                # 全ポジション強制決済
                for sym in list(positions.keys()):
                    pos = positions[sym]
                    if ts not in dfs[sym].index: continue
                    price = dfs[sym].loc[ts]["close"]
                    exit_p = price * (1 - SLIP)
                    pnl = (exit_p - pos.entry) * pos.size
                    fee = exit_p * pos.size * FEE
                    cash += max(pos.margin + pnl - fee, 0)
                    del positions[sym]

        # ポジション単位SL判定 + 清算
        for sym in list(positions.keys()):
            pos = positions[sym]
            if ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            current_pnl = (low - pos.entry) * pos.size
            eq = pos.margin + current_pnl
            loss_pct = -current_pnl / pos.margin * 100 if pos.margin > 0 else 0

            # SL判定
            if cfg.get("stop_loss_pct") and loss_pct >= cfg["stop_loss_pct"]:
                exit_p = low * (1 - SLIP)
                pnl = (exit_p - pos.entry) * pos.size
                fee = exit_p * pos.size * FEE
                cash += max(pos.margin + pnl - fee, 0)
                del positions[sym]
                stop_loss_count += 1
                continue

            # 清算
            mm = low * pos.size * MMR
            if eq <= mm:
                liquidations += 1
                del positions[sym]

        # リバランス
        if not circuit_breaker_tripped and (last_rebal is None or (ts - last_rebal).days >= 30):
            # 決済
            for sym in list(positions.keys()):
                pos = positions[sym]
                if ts not in dfs[sym].index: continue
                price = dfs[sym].loc[ts]["close"]
                exit_p = price * (1 - SLIP)
                pnl = (exit_p - pos.entry) * pos.size
                fee = exit_p * pos.size * FEE
                cash += max(pos.margin + pnl - fee, 0)
                del positions[sym]

            total = cash

            # Cooldown判定
            cooldown = False
            if cfg.get("cooldown_threshold") and last_snapshot > 0:
                period_ret = total / last_snapshot - 1
                if period_ret <= cfg["cooldown_threshold"]:
                    cooldown = True
                    cooldown_count += 1
            last_snapshot = total

            if not cooldown:
                # 現金バッファ確保
                buffer_pct = cfg.get("cash_buffer", 0)
                usable = total * (1 - buffer_pct)

                for sym, w in cfg["allocations"].items():
                    if sym not in dfs or ts not in dfs[sym].index: continue

                    hist = dfs[sym][dfs[sym].index < ts]
                    kl = compute_kelly(hist, cfg["lookback"], cfg["fraction"], cfg["max_leverage"])

                    # 閾値判定
                    min_thresh = cfg.get("min_leverage_threshold", 0.1)
                    if kl < min_thresh:
                        continue

                    # 配分方式
                    if cfg.get("use_notional_based"):
                        # Notional基準: 目標notionalから逆算
                        target_notional = usable * w
                        current_price = dfs[sym].loc[ts]["close"]
                        entry = current_price * (1 + SLIP)
                        size = target_notional / entry
                        notional = size * entry
                        margin = notional / kl  # 必要マージン
                        # マージンが上限超過なら縮小
                        if margin > usable * w * 2:  # 目標配分の2倍まで
                            margin = usable * w
                            notional = margin * kl
                            size = notional / entry
                        fee = notional * FEE
                        margin -= fee
                    else:
                        # 現行: alloc基準
                        alloc = usable * w
                        current_price = dfs[sym].loc[ts]["close"]
                        entry = current_price * (1 + SLIP)
                        notional = alloc * kl
                        size = notional / entry
                        fee = notional * FEE
                        margin = alloc - fee

                    if margin <= 0 or margin > cash: continue
                    positions[sym] = Pos(entry=entry, size=size, lev=kl, margin=margin, high_water=entry)
                    cash -= margin

            last_rebal = ts

    # 最終決済
    if all_dates and positions:
        ts = all_dates[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            if ts not in dfs[sym].index: continue
            price = dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - SLIP)
            pnl = (exit_p - pos.entry) * pos.size
            fee = exit_p * pos.size * FEE
            cash += max(pos.margin + pnl - fee, 0)

    return {
        "final": max(cash, 0), "max_dd": max_dd,
        "cooldowns": cooldown_count, "liquidations": liquidations,
        "stop_losses": stop_loss_count,
        "circuit_breaker": circuit_breaker_tripped,
    }


def eval_config(dfs, cfg, periods, label):
    rets = []
    liqs = 0
    cbs = 0
    for s, e in periods:
        r = run_improved_bot(dfs, cfg, s, e)
        months = (e - s).days / 30.0
        m = ((r["final"]/INITIAL)**(1/months)-1)*100 if r["final"] > 0 else -100
        rets.append(m)
        if r["liquidations"] > 0: liqs += 1
        if r["circuit_breaker"]: cbs += 1

    pos_rate = sum(1 for m in rets if m > 0) / len(rets) * 100
    return {
        "label": label,
        "pos_rate": pos_rate,
        "avg": np.mean(rets),
        "median": np.median(rets),
        "min": np.min(rets),
        "max": np.max(rets),
        "liqs": liqs,
        "cbs": cbs,
    }


def main():
    print(f"\n🔬 改善版ボット 比較検証")
    print(f"{'='*110}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 データ取得...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ 取得完了\n")

    # 1年ウィンドウ
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

    print(f"1年ウィンドウ: {len(windows_1y)}個  /  2年ウィンドウ: {len(windows_2y)}個\n")

    BASE = {
        "allocations": {"BNB": 0.7, "BTC": 0.3},
        "lookback": 60, "fraction": 0.5, "max_leverage": 10,
        "cooldown_threshold": -0.25,
    }

    configs = [
        ("V1: 現状 (基準)", {**BASE, "min_leverage_threshold": 0.1}),
        ("V2: Kelly<1.0スキップ", {**BASE, "min_leverage_threshold": 1.0}),
        ("V3: V2+5%現金バッファ", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05}),
        ("V4: V3+ポジSL-20%", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05, "stop_loss_pct": 20}),
        ("V5: V4+DD-35%CB", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05,
                              "stop_loss_pct": 20, "circuit_breaker_dd": 35}),
        ("V6: V5+Notional基準", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05,
                                  "stop_loss_pct": 20, "circuit_breaker_dd": 35,
                                  "use_notional_based": True}),
        # 代替パラメータ
        ("V7: SL-15%", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05, "stop_loss_pct": 15}),
        ("V8: SL-25%", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05, "stop_loss_pct": 25}),
        ("V9: CB-30%", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05, "circuit_breaker_dd": 30}),
        ("V10: CB-40%", {**BASE, "min_leverage_threshold": 1.0, "cash_buffer": 0.05, "circuit_breaker_dd": 40}),
        ("V11: 緩め min_lev=0.5", {**BASE, "min_leverage_threshold": 0.5, "cash_buffer": 0.05,
                                    "stop_loss_pct": 20}),
    ]

    # 評価
    print(f"{'='*110}")
    print(f"  📊 1年ウィンドウでの比較 ({len(windows_1y)}個)")
    print(f"{'='*110}")
    print(f"  {'戦略':<30s} {'+率':>5s} {'平均':>8s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'清算':>5s} {'CB':>4s}")
    print(f"  {'-'*90}")

    results_1y = []
    for label, cfg in configs:
        r = eval_config(dfs, cfg, windows_1y, label)
        results_1y.append(r)
        print(f"  {label:<30s} {r['pos_rate']:4.0f}% {r['avg']:+7.2f}% {r['median']:+6.2f}%  "
              f"{r['min']:+5.1f}% {r['max']:+5.1f}% {r['liqs']:>4d} {r['cbs']:>3d}")

    # 2年ウィンドウ
    print(f"\n{'='*110}")
    print(f"  📊 2年ウィンドウでの比較 ({len(windows_2y)}個)")
    print(f"{'='*110}")
    print(f"  {'戦略':<30s} {'+率':>5s} {'平均':>8s} {'中央値':>8s} {'最低':>7s} {'最高':>7s} {'清算':>5s} {'CB':>4s}")
    print(f"  {'-'*90}")

    results_2y = []
    for label, cfg in configs:
        r = eval_config(dfs, cfg, windows_2y, label)
        results_2y.append(r)
        print(f"  {label:<30s} {r['pos_rate']:4.0f}% {r['avg']:+7.2f}% {r['median']:+6.2f}%  "
              f"{r['min']:+5.1f}% {r['max']:+5.1f}% {r['liqs']:>4d} {r['cbs']:>3d}")

    # 総合スコア算出
    print(f"\n{'='*110}")
    print(f"  🏆 総合スコア (月次リターン × 安定性 - リスクペナルティ)")
    print(f"{'='*110}")
    scores = []
    for i, (label, cfg) in enumerate(configs):
        r1 = results_1y[i]
        r2 = results_2y[i]
        # スコア計算: 平均月次 × (prate/100) - ペナルティ
        score = (r1["avg"] + r2["avg"]) / 2 * (r1["pos_rate"] / 100) - r1["liqs"] * 2 - r1["cbs"] * 1
        # 最悪月が大きいほどペナルティ
        score -= abs(r1["min"]) * 0.1
        scores.append({"label": label, "score": score, "r1": r1, "r2": r2, "cfg": cfg})

    scores.sort(key=lambda x: x["score"], reverse=True)
    print(f"  {'順位':<3s} {'戦略':<30s} {'スコア':>7s} {'1年月次':>8s} {'2年月次':>8s} {'1年+率':>6s} {'2年+率':>6s}")
    for i, s in enumerate(scores, 1):
        print(f"  {i:>2d}. {s['label']:<30s} {s['score']:>6.2f}  {s['r1']['avg']:+6.2f}% {s['r2']['avg']:+6.2f}% "
              f"{s['r1']['pos_rate']:>4.0f}% {s['r2']['pos_rate']:>4.0f}%")

    # 最優秀発表
    best = scores[0]
    print(f"\n{'='*110}")
    print(f"  💎 最優秀戦略: {best['label']}")
    print(f"{'='*110}")
    print(f"  1年月次平均: {best['r1']['avg']:+.2f}% (+率 {best['r1']['pos_rate']:.0f}%)")
    print(f"  2年月次平均: {best['r2']['avg']:+.2f}% (+率 {best['r2']['pos_rate']:.0f}%)")
    print(f"  清算: {best['r1']['liqs']}期間  /  CB: {best['r1']['cbs']}期間")
    print(f"  $3,000 → 2年後期待値: ${3000 * (1+best['r2']['avg']/100)**24:,.0f}")
    print(f"\n  設定:")
    for k, v in best["cfg"].items():
        print(f"    {k}: {v}")
    print()


if __name__ == "__main__":
    main()
