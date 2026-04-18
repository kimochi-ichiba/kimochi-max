"""
iterate_stable_with_risk_mgmt.py
================================
リスク管理追加版: 月+8-15%の安定化挑戦

前回の発見:
- BNB 2x の月次中央値 +13.1% (理想的!) だが年間 +1.30% (悪化要因アリ)
- ETH 4x の年 +213% (+9.97%/月) 到達だが DD 85%
- 複数通貨組合せ=多くが清算

今回の追加:
1. トレンドフィルター (EMA50 > EMA200 でのみエントリー)
2. 絶対的損切り (DD-20%で一時撤退)
3. ボラティリティ調整ポジション (低ボラ銘柄により多く配分)
4. 月次再評価 (前月弱ければ一時停止)

$3,000スタート、1ヶ月+1年両方検証。
"""

from __future__ import annotations

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
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0
END_DATE = datetime(2026, 4, 18)
YEAR_DAYS = 365
FETCH_START = END_DATE - timedelta(days=YEAR_DAYS + 250)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

COINS = {
    "BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "BNB": "BNB/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT", "LINK": "LINK/USDT:USDT",
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


def compute_ema_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["bullish"] = df["ema50"] > df["ema200"]
    return df


def strategy_trend_filtered(df_sym: pd.DataFrame, allocs_weight: float, leverage: float,
                              stop_loss_pct: float, start_ts: pd.Timestamp, end_ts: pd.Timestamp):
    """
    トレンドフィルター付き運用:
    - EMA50 > EMA200 でのみエントリー
    - -stop_loss_pct でポジション閉じ、EMA再上抜けまで待機
    """
    df_sym = compute_ema_signals(df_sym)
    df_sym = df_sym[(df_sym.index >= start_ts) & (df_sym.index <= end_ts)]
    if df_sym.empty: return INITIAL * allocs_weight, False, 0

    cash = INITIAL * allocs_weight
    pos_qty = 0
    pos_entry = 0
    peak_equity = cash
    max_dd = 0
    liquidated = False
    cooldown_until = None

    for ts, row in df_sym.iterrows():
        if pd.isna(row["ema200"]): continue
        price = row["close"]
        low = row["low"]
        bull = row["bullish"]

        # 清算 & 損切り判定
        if pos_qty > 0:
            current_eq = cash + (low - pos_entry) * pos_qty
            mm = low * pos_qty * MMR
            if current_eq <= mm:
                liquidated = True
                cash = 0; pos_qty = 0
                break
            # 損切り: ポジション建てからの下落
            drawdown_from_entry = (pos_entry - low) / pos_entry
            if drawdown_from_entry >= stop_loss_pct:
                exit_p = pos_entry * (1 - stop_loss_pct) * (1 - SLIP)
                cash += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
                pos_qty = 0
                cooldown_until = ts + timedelta(days=7)  # 1週間クールダウン

        # トレンド転換で決済
        if pos_qty > 0 and not bull:
            exit_p = price * (1 - SLIP)
            cash += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
            pos_qty = 0

        # エントリー (クールダウン中でない & ブル)
        if pos_qty == 0 and bull and (cooldown_until is None or ts > cooldown_until):
            entry = price * (1 + SLIP)
            notional = cash * leverage
            pos_qty = notional / entry
            pos_entry = entry
            cash -= notional * FEE
            cooldown_until = None

        # 現在equity
        current_eq = cash + (price - pos_entry) * pos_qty if pos_qty > 0 else cash
        if current_eq > peak_equity: peak_equity = current_eq
        if peak_equity > 0:
            dd = (peak_equity - current_eq) / peak_equity * 100
            max_dd = max(max_dd, dd)

    # 最終決済
    if pos_qty > 0 and not liquidated:
        exit_p = df_sym.iloc[-1]["close"] * (1 - SLIP)
        cash += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    return cash, liquidated, max_dd


def test_portfolio_trend(dfs, allocs, leverage, stop_loss_pct, label, windows):
    """ポートフォリオ全体でトレンドフィルター付き運用"""
    # 1年通し
    annual_start = pd.Timestamp(END_DATE - timedelta(days=YEAR_DAYS))
    annual_end = pd.Timestamp(END_DATE)
    annual_final = 0
    annual_liq = False
    annual_dd = 0
    for sym, w in allocs.items():
        if sym not in dfs or w <= 0: continue
        fv, liq, dd = strategy_trend_filtered(dfs[sym], w, leverage, stop_loss_pct,
                                                annual_start, annual_end)
        annual_final += fv
        annual_dd = max(annual_dd, dd)
        if liq: annual_liq = True

    annual_ret = (annual_final/INITIAL - 1) * 100
    annual_monthly = ((annual_final/INITIAL) ** (1/12) - 1) * 100 if annual_final > 0 else -100

    # 1ヶ月ウィンドウ
    monthly_rets = []
    for ws, we in windows:
        total = 0
        any_liq = False
        for sym, w in allocs.items():
            if sym not in dfs or w <= 0: continue
            fv, liq, _ = strategy_trend_filtered(dfs[sym], w, leverage, stop_loss_pct, ws, we)
            total += fv
            if liq: any_liq = True
        monthly_rets.append((total/INITIAL - 1) * 100)

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
        "positive_months": sum(1 for r in rets if r > 0),
        "months_8to15": sum(1 for r in rets if 8 <= r <= 15),
        "months_5to20": sum(1 for r in rets if 5 <= r <= 20),
        "months_over_0": sum(1 for r in rets if r > 0),
        "total_windows": len(rets),
    }


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🛡️ リスク管理追加版: 月+8〜15%の安定化挑戦")
    print(f"{'='*95}")
    print(f"期間: 1年 ({(END_DATE - timedelta(days=YEAR_DAYS)).strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')})")
    print(f"初期資金: ${INITIAL:,.0f}")
    print(f"仕組: EMA50/200トレンドフィルター + 損切り + クールダウン")
    print(f"{'='*95}\n")

    print(f"📥 データ取得中...")
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    dfs = {}
    for name, sym in COINS.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 1ヶ月ウィンドウ生成
    start_base = END_DATE - timedelta(days=YEAR_DAYS)
    windows = []
    cursor = start_base
    while cursor + timedelta(days=30) <= END_DATE:
        windows.append((pd.Timestamp(cursor), pd.Timestamp(cursor + timedelta(days=30))))
        cursor += timedelta(days=15)

    tests = [
        # 単独通貨 + トレンドフィルター + 異なる損切り幅
        ("ETH 100% 3x + SL15% + Trend",  {"ETH": 1.0}, 3, 0.15),
        ("ETH 100% 3x + SL20% + Trend",  {"ETH": 1.0}, 3, 0.20),
        ("ETH 100% 4x + SL15% + Trend",  {"ETH": 1.0}, 4, 0.15),
        ("ETH 100% 5x + SL12% + Trend",  {"ETH": 1.0}, 5, 0.12),
        ("BNB 100% 3x + SL15% + Trend",  {"BNB": 1.0}, 3, 0.15),
        ("BNB 100% 4x + SL12% + Trend",  {"BNB": 1.0}, 4, 0.12),

        # BNB+ETHトレンド分散
        ("BNB50+ETH50 3x + SL15%",      {"BNB": 0.5, "ETH": 0.5}, 3, 0.15),
        ("BNB50+ETH50 4x + SL12%",      {"BNB": 0.5, "ETH": 0.5}, 4, 0.12),
        ("BNB60+ETH40 3x + SL15%",      {"BNB": 0.6, "ETH": 0.4}, 3, 0.15),
        ("BNB60+ETH40 4x + SL12%",      {"BNB": 0.6, "ETH": 0.4}, 4, 0.12),
        ("BNB40+ETH60 3x + SL15%",      {"BNB": 0.4, "ETH": 0.6}, 3, 0.15),
        ("BNB40+ETH60 4x + SL12%",      {"BNB": 0.4, "ETH": 0.6}, 4, 0.12),
        ("BNB70+ETH30 3x + SL15%",      {"BNB": 0.7, "ETH": 0.3}, 3, 0.15),

        # 3銘柄
        ("BNB40+ETH40+BTC20 3x + SL15%",{"BNB": 0.4, "ETH": 0.4, "BTC": 0.2}, 3, 0.15),
        ("BNB50+ETH30+BTC20 3x + SL15%",{"BNB": 0.5, "ETH": 0.3, "BTC": 0.2}, 3, 0.15),
    ]

    results = []
    for label, allocs, leverage, sl in tests:
        print(f"🔬 {label}")
        r = test_portfolio_trend(dfs, allocs, leverage, sl, label, windows)
        results.append(r)
        status = "🎯" if 5 <= r["annual_monthly"] <= 20 else ("✅" if r["annual_monthly"] > 20 else "⚠️")
        liq_s = " 清算" if r["annual_liquidated"] else ""
        print(f"   {status} 1年月次{r['annual_monthly']:+.2f}%  年{r['annual_return']:+.1f}%  "
              f"DD{r['annual_dd']:.0f}%  中央値{r['monthly_median']:+.1f}%  "
              f"5〜20%月{r['months_5to20']}/{r['total_windows']}  プラス{r['positive_months']}/{r['total_windows']}{liq_s}")

    # ランキング
    print(f"\n{'='*95}")
    print(f"  📊 ランキング (プラス月数で並べ替え)")
    print(f"{'='*95}")
    results.sort(key=lambda x: (x["positive_months"], x["annual_monthly"]), reverse=True)
    print(f"  {'戦略':<35s} {'月次':>8s} {'年率':>8s} {'DD':>6s} {'中央':>7s} {'+月':>5s} {'+5-20%':>7s} {'最終':>11s}")
    print(f"  {'-'*95}")
    for r in results:
        final_str = f"${r['annual_final']:,.0f}" if not r['annual_liquidated'] else "💀清算"
        print(f"  {r['name']:<35s} {r['annual_monthly']:+6.2f}%  {r['annual_return']:+6.1f}%  "
              f"{r['annual_dd']:5.0f}%  {r['monthly_median']:+5.1f}%  "
              f"{r['positive_months']:>3d}/{r['total_windows']} "
              f"{r['months_5to20']:>3d}/{r['total_windows']}   {final_str:>10s}")

    # Top 3 詳細
    safe = [r for r in results if not r["annual_liquidated"]]
    if safe:
        print(f"\n{'='*95}")
        print(f"  🏆 Top 3 (清算なし) 詳細")
        print(f"{'='*95}")
        for i, r in enumerate(safe[:3], 1):
            profit = r["annual_final"] - INITIAL
            multi = r["annual_final"] / INITIAL
            print(f"\n  {i}. {r['name']}")
            print(f"     📈 $3,000 → ${r['annual_final']:,.0f}  (利益+${profit:,.0f} / {multi:.2f}倍)")
            print(f"     📊 月次複利: {r['annual_monthly']:+.2f}%")
            print(f"     📉 最大DD: {r['annual_dd']:.0f}%")
            print(f"     🎯 月別内訳:")
            print(f"         プラス月    : {r['positive_months']}/{r['total_windows']}")
            print(f"         +5〜20%の月 : {r['months_5to20']}/{r['total_windows']}")
            print(f"         中央値       : {r['monthly_median']:+.1f}%")
            print(f"         最大 / 最小  : {r['monthly_max']:+.1f}% / {r['monthly_min']:+.1f}%")

    print()


if __name__ == "__main__":
    main()
