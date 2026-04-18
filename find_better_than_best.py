"""
find_better_than_best.py
========================
現在の1位 (BNB70+BTC30, 月+10.28%) を超える戦略を探索

前回の知見:
- BTC単独: 97% positive (最高安全性) だが月+6.86%
- BNB単独: 81% positive, 月+9.51%
- BNB70+BTC30: 92% positive, 月+10.28%
- BNB80+BTC20: 83% positive, 月+9.31%
- BNB+AVAX/SOL/LINK: 清算多発

戦略バリエーション:
1. 配分変更 (BNB比率調整)
2. Kelly fraction 調整
3. Max leverage 調整
4. Lookback 調整
5. Rebalance 頻度調整
6. ETH追加の3銘柄
7. 安定装置追加 (Cooldown, Stop-loss)
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
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


def compute_kelly(df_hist: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> float:
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
    high_water: float = 0  # For stop loss


def run_strategy(dfs: Dict[str, pd.DataFrame], weights: Dict[str, float],
                  lookback: int, fraction: float, max_lev: float, rebal_days: int,
                  start: datetime, end: datetime,
                  stop_loss_pct: float = 0,   # ポジション単位の損切り
                  cooldown_threshold: float = 0,  # 月次-Xx以下で次月スキップ
                  ):
    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL
    cooldown = False
    liquidations = 0

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    peak = INITIAL
    max_dd = 0

    for ts in all_dates:
        # 清算 + Stop Loss
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym not in dfs or ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            current_pnl = (low - pos.entry) * pos.size
            eq = pos.margin + current_pnl

            # Stop Loss (margin割合で)
            if stop_loss_pct > 0 and pos.margin > 0:
                loss_pct = -current_pnl / pos.margin if pos.margin > 0 else 0
                if loss_pct >= stop_loss_pct:
                    # 損切り
                    price = dfs[sym].loc[ts]["close"]
                    exit_p = price * (1 - SLIP)
                    pnl = (exit_p - pos.entry) * pos.size
                    fee = exit_p * pos.size * FEE
                    cash += max(pos.margin + pnl - fee, 0)
                    del positions[sym]
                    continue

            # 清算チェック
            mm = low * pos.size * MMR
            if eq <= mm:
                liquidations += 1
                del positions[sym]

        # リバランス
        if last_rebal is None or (ts - last_rebal).days >= rebal_days:
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

            # Cooldown check
            if cooldown_threshold < 0 and last_snapshot > 0:
                month_ret = total / last_snapshot - 1
                if month_ret <= cooldown_threshold:
                    cooldown = True
                else:
                    cooldown = False
            last_snapshot = total

            if not cooldown:
                for sym, w in weights.items():
                    if sym not in dfs or ts not in dfs[sym].index: continue
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl = compute_kelly(hist, lookback, fraction, max_lev)
                    if kl < 0.1: continue
                    alloc = total * w
                    current = dfs[sym].loc[ts]["close"]
                    entry = current * (1 + SLIP)
                    notional = alloc * kl
                    size = notional / entry
                    fee = notional * FEE
                    margin = alloc - fee
                    positions[sym] = Pos(entry=entry, size=size, lev=kl, margin=margin, high_water=entry)
                    cash -= margin

            last_rebal = ts

        # Equity追跡
        eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                if p > pos.high_water: pos.high_water = p
                eq += pos.margin + (p - pos.entry) * pos.size
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
            if ts not in dfs[sym].index: continue
            price = dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - SLIP)
            pnl = (exit_p - pos.entry) * pos.size
            fee = exit_p * pos.size * FEE
            cash += max(pos.margin + pnl - fee, 0)

    return max(cash, 0), liquidations, max_dd


def eval_strategy(dfs, cfg, windows_1y, windows_2y, label):
    """戦略評価: 1年と2年ウィンドウの統計を返す"""
    rets_1y, liqs_1y = [], 0
    for s, e in windows_1y:
        final, liq, _ = run_strategy(
            dfs, cfg["weights"], cfg["lookback"], cfg["fraction"],
            cfg["max_lev"], cfg["rebal_days"], s, e,
            cfg.get("stop_loss_pct", 0), cfg.get("cooldown_threshold", 0),
        )
        months = (e - s).days / 30.0
        m = ((final/INITIAL)**(1/months)-1)*100 if final > 0 else -100
        rets_1y.append(m)
        if liq > 0: liqs_1y += 1

    rets_2y, liqs_2y = [], 0
    for s, e in windows_2y:
        final, liq, _ = run_strategy(
            dfs, cfg["weights"], cfg["lookback"], cfg["fraction"],
            cfg["max_lev"], cfg["rebal_days"], s, e,
            cfg.get("stop_loss_pct", 0), cfg.get("cooldown_threshold", 0),
        )
        months = (e - s).days / 30.0
        m = ((final/INITIAL)**(1/months)-1)*100 if final > 0 else -100
        rets_2y.append(m)
        if liq > 0: liqs_2y += 1

    pos_1y = sum(1 for m in rets_1y if m > 0) / len(rets_1y) * 100
    pos_2y = sum(1 for m in rets_2y if m > 0) / len(rets_2y) * 100 if rets_2y else 0

    return {
        "label": label,
        "pos_1y": pos_1y,
        "avg_1y": np.mean(rets_1y),
        "median_1y": np.median(rets_1y),
        "min_1y": np.min(rets_1y),
        "pos_2y": pos_2y,
        "avg_2y": np.mean(rets_2y) if rets_2y else 0,
        "median_2y": np.median(rets_2y) if rets_2y else 0,
        "min_2y": np.min(rets_2y) if rets_2y else 0,
        "liqs_1y_periods": liqs_1y,
        "liqs_2y_periods": liqs_2y,
    }


def main():
    print(f"\n🎯 月次リターン更なる向上を探す検証")
    print(f"{'='*100}")
    print(f"基準: Kelly BNB70+BTC30  lb60 frac0.5 max10 rebal30 (1年 +10.28%, 2年 +11.76%)\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 データ取得...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT"), ("ETH","ETH/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨\n")

    # ウィンドウ生成
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

    # 戦略候補リスト
    configs = [
        # 基準
        {"label": "★基準: BNB70+BTC30 lb60 frac0.5 max10",
         "weights": {"BNB": 0.7, "BTC": 0.3}, "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},

        # 配分変更
        {"label": "BNB80+BTC20", "weights": {"BNB": 0.8, "BTC": 0.2},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB90+BTC10", "weights": {"BNB": 0.9, "BTC": 0.1},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB60+BTC40 (BTC多め)", "weights": {"BNB": 0.6, "BTC": 0.4},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB50+BTC50 (均等)", "weights": {"BNB": 0.5, "BTC": 0.5},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB100 (単独)", "weights": {"BNB": 1.0},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},

        # Max Leverage 変更
        {"label": "BNB70+BTC30 max12", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 12, "rebal_days": 30},
        {"label": "BNB70+BTC30 max15", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 15, "rebal_days": 30},

        # Fraction 変更
        {"label": "BNB70+BTC30 frac0.6", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.6, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB70+BTC30 frac0.4 (保守)", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.4, "max_lev": 10, "rebal_days": 30},

        # Lookback 変更
        {"label": "BNB70+BTC30 lb45", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 45, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB70+BTC30 lb90", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 90, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},

        # Rebalance 変更
        {"label": "BNB70+BTC30 rebal15d", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 15},
        {"label": "BNB70+BTC30 rebal45d", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 45},

        # 3銘柄
        {"label": "BNB50+ETH30+BTC20 (3コイン)", "weights": {"BNB": 0.5, "ETH": 0.3, "BTC": 0.2},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},
        {"label": "BNB60+ETH20+BTC20", "weights": {"BNB": 0.6, "ETH": 0.2, "BTC": 0.2},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30},

        # 安全装置追加 (Stop Loss)
        {"label": "BNB70+BTC30 + SL -30%", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30,
         "stop_loss_pct": 0.30},
        {"label": "BNB70+BTC30 + SL -50%", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30,
         "stop_loss_pct": 0.50},

        # Cooldown追加
        {"label": "BNB70+BTC30 + Cooldown-25%", "weights": {"BNB": 0.7, "BTC": 0.3},
         "lookback": 60, "fraction": 0.5, "max_lev": 10, "rebal_days": 30,
         "cooldown_threshold": -0.25},

        # 組み合わせ
        {"label": "BNB80+BTC20 max12", "weights": {"BNB": 0.8, "BTC": 0.2},
         "lookback": 60, "fraction": 0.5, "max_lev": 12, "rebal_days": 30},
        {"label": "BNB80+BTC20 max12 +SL30%", "weights": {"BNB": 0.8, "BTC": 0.2},
         "lookback": 60, "fraction": 0.5, "max_lev": 12, "rebal_days": 30,
         "stop_loss_pct": 0.30},
    ]

    # 実行
    results = []
    for i, cfg in enumerate(configs, 1):
        print(f"[{i:2d}/{len(configs)}] {cfg['label']}")
        r = eval_strategy(dfs, cfg, windows_1y, windows_2y, cfg["label"])
        results.append(r)

    # ランキング表示
    print(f"\n{'='*110}")
    print(f"  🏆 戦略ランキング (2年月次平均で並べ替え)")
    print(f"{'='*110}")
    print(f"  {'戦略':<38s} {'1Y+率':>6s} {'1Y月次':>8s} {'1Y最低':>7s} {'2Y+率':>6s} {'2Y月次':>8s} {'2Y最低':>7s} {'清算':>6s}")
    print(f"  {'-'*100}")

    # 2年月次でソート (清算多いものはペナルティ)
    results.sort(key=lambda x: x["avg_2y"] - x["liqs_1y_periods"] * 2 - x["liqs_2y_periods"] * 5, reverse=True)

    for r in results:
        star = "★" if "基準" in r["label"] else " "
        print(f"  {star}{r['label']:<37s} {r['pos_1y']:4.0f}%  {r['avg_1y']:+6.2f}%  {r['min_1y']:+5.1f}%  "
              f"{r['pos_2y']:4.0f}%  {r['avg_2y']:+6.2f}%  {r['min_2y']:+5.1f}%  "
              f"{r['liqs_1y_periods']}/{r['liqs_2y_periods']}")

    # 基準との比較
    baseline = next((r for r in results if "基準" in r["label"]), None)
    print(f"\n{'='*110}")
    print(f"  💡 基準戦略(BNB70+BTC30)より優れた戦略")
    print(f"{'='*110}")
    if baseline:
        print(f"  基準: 2年月次 {baseline['avg_2y']:+.2f}%  プラス率 {baseline['pos_2y']:.0f}%  清算 {baseline['liqs_2y_periods']}")
        print()

        better = [r for r in results if r["avg_2y"] > baseline["avg_2y"]
                   and r["liqs_2y_periods"] <= baseline["liqs_2y_periods"]
                   and r["pos_2y"] >= baseline["pos_2y"] - 10  # プラス率10%以内低下まで許容
                   and "基準" not in r["label"]]
        if better:
            for r in better[:5]:
                diff = r["avg_2y"] - baseline["avg_2y"]
                print(f"  ✅ {r['label']}: 2年月次{r['avg_2y']:+.2f}% (基準より{diff:+.2f}%), "
                      f"+率{r['pos_2y']:.0f}%, 清算{r['liqs_2y_periods']}")
        else:
            print(f"  ⚠️ より安全でより高リターンの戦略は見つかりませんでした")
            print(f"  → 基準のBNB70+BTC30が最適解の可能性が高い")

    # 最優秀ピック
    print(f"\n{'='*110}")
    top_safe = [r for r in results if r["liqs_2y_periods"] == 0 and r["pos_2y"] >= 85]
    top_safe.sort(key=lambda x: x["avg_2y"], reverse=True)
    if top_safe:
        best = top_safe[0]
        print(f"  🏆 最優秀 (清算なし + プラス率85%以上): {best['label']}")
        print(f"     2年月次 {best['avg_2y']:+.2f}%  プラス率 {best['pos_2y']:.0f}%  最低月次 {best['min_2y']:+.2f}%")
        print(f"     $3,000 → 2年後期待値 平均${3000*(1+best['avg_2y']/100)**24:,.0f}")
    print()


if __name__ == "__main__":
    main()
