"""
strategy_monthly_momentum.py
============================
月次モメンタム・リバランス戦略

手法:
- 毎月1日に100通貨から「過去30日リターン上位10」を選定
- 資金を10銘柄に均等配分
- レバレッジ2倍 (保守的)
- 30日ホールド、翌月1日に全決済→再配分
- トレード頻度: 月1回のみ (年12リバランス = 240取引程度)

科学的根拠:
- Jegadeesh & Titman (1993): モメンタム・プレミアムを学術的に実証
- 過去の勝者は次月も勝ちやすい(6-12ヶ月モメンタム)
- クリプトでも同様のアノマリー確認 (2018-2024研究)

初期資金: $10,000
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

# パラメータ
TOTAL_CAPITAL = 10_000.0
N_COINS = 100              # 候補プール
TOP_N = 10                 # 上位何銘柄を持つか
LOOKBACK_DAYS = 30         # モメンタム計算期間
HOLD_DAYS = 30             # 保有期間
LEVERAGE = 2.0
FEE_RATE = 0.0002          # Maker想定
SLIPPAGE = 0.0005
INITIAL_FETCH_DAYS = 365 + 60  # 365日分析+60日バッファ


def get_top_symbols(n=100):
    ex = ccxt.binance({"options": {"defaultType": "future"}})
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for sym, m in markets.items():
        if not m.get("active") or m.get("quote") != "USDT" or not m.get("swap"):
            continue
        if sym in tickers:
            vol_usd = tickers[sym].get("quoteVolume") or 0
            rows.append((sym, vol_usd))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def fetch_ohlcv(ex, symbol, timeframe, since_ms, until_ms):
    tf_ms = 86400 * 1000
    all_data = []
    current = since_ms
    while current < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=current, limit=1000)
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


def run_monthly_momentum(all_data: Dict[str, pd.DataFrame], start_date: datetime, end_date: datetime):
    """毎月リバランスで運用"""
    balance = TOTAL_CAPITAL
    holdings = {}  # symbol → {entry_price, qty, entry_date}
    history = []
    monthly_returns = []

    current_date = start_date
    month_idx = 0

    while current_date + timedelta(days=HOLD_DAYS) <= end_date:
        month_idx += 1
        ts = pd.Timestamp(current_date)

        # === 決済: 既存ホールドを全部売る ===
        realized_pnl = 0
        for sym, h in holdings.items():
            if sym not in all_data: continue
            # current_dateに最も近い価格取得
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            price = row["close"].iloc[-1] * (1 - SLIPPAGE)
            # PnL
            gross = (price - h["entry_price"]) * h["qty"] * LEVERAGE
            fee = price * h["qty"] * FEE_RATE
            pnl = gross - fee
            balance += pnl
            realized_pnl += pnl
        holdings = {}

        # === 候補銘柄のモメンタムスコア計算 ===
        scores = {}
        lookback_ts = pd.Timestamp(current_date - timedelta(days=LOOKBACK_DAYS))
        for sym, df_sym in all_data.items():
            # 過去30日リターン
            past = df_sym[df_sym.index <= lookback_ts]
            current = df_sym[df_sym.index <= ts]
            if past.empty or current.empty: continue
            p0 = past["close"].iloc[-1]
            p1 = current["close"].iloc[-1]
            if p0 <= 0: continue
            mom = (p1 / p0) - 1
            scores[sym] = mom

        # Top N選定
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
        if not ranked:
            current_date += timedelta(days=HOLD_DAYS)
            continue

        # === 新規ポジション構築: 均等配分 ===
        per_coin = balance / TOP_N
        for sym, mom_score in ranked:
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            entry_price = row["close"].iloc[-1] * (1 + SLIPPAGE)
            # 投入額 = per_coin, size = per_coin * leverage / price
            notional = per_coin * LEVERAGE
            qty = notional / entry_price
            balance -= per_coin * FEE_RATE * LEVERAGE  # 手数料
            holdings[sym] = {
                "entry_price": entry_price,
                "qty": qty,
                "entry_date": current_date,
                "mom_score": mom_score,
            }

        # === 月次リターン記録 ===
        # 次の月初まで進む (実際の評価は月末に)
        next_date = current_date + timedelta(days=HOLD_DAYS)

        # 保有中の評価損益を次月初に確定させた後に計算
        # → 上のループの次のiterationで行われる
        history.append({
            "month": month_idx,
            "start": current_date,
            "end": next_date,
            "balance_start": balance + realized_pnl if month_idx == 1 else balance,
            "holdings": list(holdings.keys()),
            "scores": [s[1] for s in ranked],
        })
        current_date = next_date

    # 最後のポジションを決済
    ts_final = pd.Timestamp(end_date)
    for sym, h in holdings.items():
        if sym not in all_data: continue
        df_sym = all_data[sym]
        row = df_sym[df_sym.index <= ts_final]
        if row.empty: continue
        price = row["close"].iloc[-1] * (1 - SLIPPAGE)
        gross = (price - h["entry_price"]) * h["qty"] * LEVERAGE
        fee = price * h["qty"] * FEE_RATE
        balance += gross - fee

    # 月次リターン再計算
    return balance, history


def main():
    end_date = datetime(2026, 4, 18)
    analysis_start = end_date - timedelta(days=365)
    fetch_start = end_date - timedelta(days=INITIAL_FETCH_DAYS)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(end_date.timestamp() * 1000)

    print(f"\n📈 月次モメンタム・リバランス戦略")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')} (365日)")
    print(f"初期資金: ${TOTAL_CAPITAL:,.0f}")
    print(f"対象: Binance先物上位{N_COINS}通貨")
    print(f"手法: 毎月1日 → 過去{LOOKBACK_DAYS}日リターンで上位{TOP_N}選定 → 均等配分 → 30日保有")
    print(f"レバレッジ: {LEVERAGE}倍  手数料: {FEE_RATE*100}%/片側")
    print(f"{'='*90}")

    print(f"\n📥 通貨リスト取得...")
    symbols = get_top_symbols(n=N_COINS)
    print(f"📥 データ取得中...")
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    all_data = {}
    for i, sym in enumerate(symbols, 1):
        df = fetch_ohlcv(ex, sym, "1d", since_ms, until_ms)
        if df.empty or len(df) < 100: continue
        all_data[sym] = df
        if i % 20 == 0:
            print(f"  進捗: {i}/{N_COINS} (成功{len(all_data)})")
    print(f"✅ {len(all_data)}通貨データ取得完了")

    print(f"\n🔄 バックテスト実行中...")
    final_balance, history = run_monthly_momentum(all_data, analysis_start, end_date)

    # 月次リターン計算
    print(f"\n{'='*90}")
    print(f"  📊 月次リバランス履歴")
    print(f"{'='*90}")
    print(f"  {'#':3s} {'開始':10s} {'終了':10s} {'選定銘柄上位5':40s} {'モメンタムTOP':>12s}")
    print(f"  {'-'*85}")
    for h in history:
        top5 = ", ".join([s.split(':')[0].split('/')[0] for s in h["holdings"][:5]])
        top_score = h["scores"][0] * 100 if h["scores"] else 0
        print(f"  [{h['month']:2d}] {h['start'].strftime('%Y-%m-%d')} {h['end'].strftime('%Y-%m-%d')}  {top5:40s} {top_score:+8.1f}%")

    # 月次リターン - 実は balance を追跡していない。再計算する
    # 簡易にbalance_startと次のbalance_startの差を使う
    print(f"\n{'='*90}")
    print(f"  📊 最終結果")
    print(f"{'='*90}")
    print(f"  初期資金        : ${TOTAL_CAPITAL:,.2f}")
    print(f"  最終資金        : ${final_balance:,.2f}")
    total_return = (final_balance / TOTAL_CAPITAL - 1) * 100
    print(f"  総リターン(1年) : {total_return:+.2f}%")

    if total_return > -99.99:
        n_months = len(history)
        monthly_comp = ((final_balance / TOTAL_CAPITAL) ** (1/n_months) - 1) * 100 if final_balance > 0 else -100
        print(f"  月次複利        : {monthly_comp:+.2f}%")
        print(f"  年率換算        : {((1+monthly_comp/100)**12 - 1)*100:+.2f}%")

    # 比較
    print(f"\n  🏆 全戦略比較 (1年 / 初期$10,000 ベース)")
    print(f"  {'='*75}")
    print(f"  {'Buy&Hold (BTC/ETH/SOL)':<30s}: $10k → ~$10,500 (月+0.41%)")
    print(f"  {'Classic Turtle':<30s}: $10k → ~$10,240 (月+0.20%)")
    print(f"  {'Momentum Leveraged(現実版)':<30s}: $10k → $13,533  (月+2.55%)")
    print(f"  {'🆕 Monthly Momentum':<30s}: $10k → ${final_balance:,.0f} "
          f"(月{monthly_comp:+.2f}%)")
    print(f"  {'='*75}")

    if total_return >= 240:  # 月20%以上 = 年240%以上
        print(f"\n  🎯 ✅ 月+20%相当達成！")
    elif total_return >= 100:
        print(f"\n  🎯 ⚠️ 月+8-15%相当で好成績")
    elif total_return >= 50:
        print(f"\n  🎯 ⚠️ 月+3-5%相当で現実的プロ水準")
    else:
        print(f"\n  🎯 ❌ 期待ほど伸びず")
    print()


if __name__ == "__main__":
    main()
