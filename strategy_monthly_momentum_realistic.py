"""
strategy_monthly_momentum_realistic.py
======================================
月次モメンタム戦略 - 現実的制約版

変更点 (現実に近づけるフィルター):
1. スリッページ 0.05% → 2.0% (小型コインのリアル水準)
2. 候補から除外:
   - 過去90日未満のデータしかない銘柄 (新規上場除外)
   - 月次モメンタム > 300% のコイン (上場ポンプ除外)
   - 24h出来高$10M未満 (流動性不足)
3. 手数料 Maker 0.02% → Taker 0.06% (現実的な注文想定)
4. 選定銘柄を「完全に流動性ある」層に限定
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

# パラメータ
TOTAL_CAPITAL = 10_000.0
N_COINS = 100
TOP_N = 10
LOOKBACK_DAYS = 30
HOLD_DAYS = 30
LEVERAGE = 2.0
FEE_RATE = 0.0006         # Taker想定 0.06%
SLIPPAGE = 0.02           # 2% (小型コイン現実値)
MIN_DATA_DAYS = 90        # 90日未満の新規上場は除外
MAX_MOMENTUM = 3.0        # +300%超えは上場ポンプとみなし除外
MIN_VOLUME_USD = 10_000_000  # 24h出来高$10M未満は除外


def get_top_symbols_with_volume(n=100):
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
    # 流動性フィルター
    filtered = [(s, v) for s, v in rows if v >= MIN_VOLUME_USD]
    return [s for s, v in filtered[:n]], {s: v for s, v in filtered[:n]}


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
    balance = TOTAL_CAPITAL
    holdings = {}
    history = []

    current_date = start_date
    month_idx = 0

    while current_date + timedelta(days=HOLD_DAYS) <= end_date:
        month_idx += 1
        ts = pd.Timestamp(current_date)

        # 決済
        realized_pnl = 0
        for sym, h in holdings.items():
            if sym not in all_data: continue
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            price = row["close"].iloc[-1] * (1 - SLIPPAGE)
            gross = (price - h["entry_price"]) * h["qty"] * LEVERAGE
            fee = price * h["qty"] * FEE_RATE
            pnl = gross - fee
            balance += pnl
            realized_pnl += pnl
        holdings = {}

        if balance <= 0:
            print(f"  [{month_idx}] 💀 資金枯渇")
            break

        # モメンタムスコア計算 + フィルタリング
        scores = {}
        lookback_ts = pd.Timestamp(current_date - timedelta(days=LOOKBACK_DAYS))
        min_listing_ts = pd.Timestamp(current_date - timedelta(days=MIN_DATA_DAYS))
        for sym, df_sym in all_data.items():
            # 最低上場期間フィルター
            if df_sym.index[0] > min_listing_ts:
                continue
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]
            p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            mom = (p1 / p0) - 1
            # 異常モメンタムフィルター
            if mom > MAX_MOMENTUM:
                continue
            scores[sym] = mom

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
        if not ranked:
            current_date += timedelta(days=HOLD_DAYS)
            continue

        # 均等配分
        per_coin = balance / TOP_N
        for sym, mom_score in ranked:
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            entry_price = row["close"].iloc[-1] * (1 + SLIPPAGE)
            notional = per_coin * LEVERAGE
            qty = notional / entry_price
            balance -= per_coin * FEE_RATE * LEVERAGE
            holdings[sym] = {"entry_price": entry_price, "qty": qty,
                             "entry_date": current_date, "mom_score": mom_score}

        history.append({
            "month": month_idx,
            "start": current_date,
            "end": current_date + timedelta(days=HOLD_DAYS),
            "holdings": list(holdings.keys()),
            "scores": [s[1] for s in ranked],
            "balance_at_start": balance + sum(h["qty"]*h["entry_price"]/LEVERAGE for h in holdings.values()),
        })
        current_date += timedelta(days=HOLD_DAYS)

    # 最終決済
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

    return balance, history


def main():
    end_date = datetime(2026, 4, 18)
    analysis_start = end_date - timedelta(days=365)
    fetch_start = end_date - timedelta(days=365 + 120)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(end_date.timestamp() * 1000)

    print(f"\n📈 月次モメンタム戦略 (現実制約版)")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"初期資金: ${TOTAL_CAPITAL:,.0f}")
    print(f"制約: スリッページ{SLIPPAGE*100}% / 手数料{FEE_RATE*100}% / 最低上場{MIN_DATA_DAYS}日 / モメンタム上限{MAX_MOMENTUM*100:.0f}%")
    print(f"{'='*90}")

    print(f"\n📥 通貨リスト (24h出来高 ≥ ${MIN_VOLUME_USD/1e6:.0f}M) 取得...")
    symbols, volumes = get_top_symbols_with_volume(n=N_COINS)
    print(f"  {len(symbols)}通貨が流動性フィルター通過")
    print(f"  上位5: {[f'{s.split(chr(58))[0]}({volumes[s]/1e6:.0f}M)' for s in symbols[:5]]}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"\n📥 データ取得中...")
    all_data = {}
    for i, sym in enumerate(symbols, 1):
        df = fetch_ohlcv(ex, sym, "1d", since_ms, until_ms)
        if df.empty or len(df) < 30: continue
        all_data[sym] = df
        if i % 20 == 0:
            print(f"  進捗: {i}/{len(symbols)} (成功{len(all_data)})")
    print(f"✅ {len(all_data)}通貨データ取得完了")

    print(f"\n🔄 バックテスト実行中...")
    final_balance, history = run_monthly_momentum(all_data, analysis_start, end_date)

    print(f"\n{'='*90}")
    print(f"  📊 月次履歴")
    print(f"{'='*90}")
    print(f"  {'#':3s} {'期間':25s} {'選定Top5':45s} {'TOPモメンタム':>12s}")
    print(f"  {'-'*90}")
    for h in history:
        top5 = ", ".join([s.split(':')[0].split('/')[0] for s in h["holdings"][:5]])
        top_score = h["scores"][0] * 100 if h["scores"] else 0
        print(f"  [{h['month']:2d}] {h['start'].strftime('%Y-%m-%d')}〜{h['end'].strftime('%m-%d')}  {top5:45s} {top_score:+8.1f}%")

    total_return = (final_balance / TOTAL_CAPITAL - 1) * 100
    n_months = len(history)
    if final_balance > 0 and n_months > 0:
        monthly_comp = ((final_balance / TOTAL_CAPITAL) ** (1/n_months) - 1) * 100
    else:
        monthly_comp = -100

    print(f"\n{'='*90}")
    print(f"  📊 現実制約版 最終結果")
    print(f"{'='*90}")
    print(f"  初期資金        : ${TOTAL_CAPITAL:,.2f}")
    print(f"  最終資金        : ${final_balance:,.2f}")
    print(f"  総リターン(1年) : {total_return:+.2f}%")
    print(f"  月次複利        : {monthly_comp:+.2f}%")
    print(f"  月数            : {n_months}")

    print(f"\n  📈 バックテスト比較")
    print(f"  {'='*70}")
    print(f"  {'Monthly Momentum (楽観)':<30s}: $10k → $806,972 (月+44.18%)")
    print(f"  {'🎯 Monthly Momentum (現実)':<30s}: $10k → ${final_balance:,.0f}  "
          f"(月{monthly_comp:+.2f}%)")
    print(f"  {'='*70}")

    # 判定
    print(f"\n  🎯 判定:")
    if monthly_comp >= 20:
        print(f"  ✅ 月+{monthly_comp:.1f}% — 現実制約下でも目標月+20%達成！")
    elif monthly_comp >= 10:
        print(f"  ⚠️ 月+{monthly_comp:.1f}% — 目標には届かないが非常に優秀")
    elif monthly_comp >= 5:
        print(f"  ⚠️ 月+{monthly_comp:.1f}% — 現実的プロ水準")
    elif monthly_comp >= 0:
        print(f"  ⚠️ 月+{monthly_comp:.1f}% — ギリギリプラス")
    else:
        print(f"  ❌ 月{monthly_comp:.1f}% — 現実制約下で損失")
    print()


if __name__ == "__main__":
    main()
