"""
Iter43 ハルシネーション検証
============================
キャッシュデータ (Binance) と CoinGecko 公開価格を比較し、
Iter43の結果が実データに基づくものか確認する。

検証項目:
  1. 50銘柄の実在確認 (キャッシュに入っているか)
  2. BTC/ETH/SOL の主要マイルストーン日の価格一致 (CoinGecko API)
  3. BTC/ETH 年別リターンの独立計算 vs Iter43 R01/R02 結果
  4. モメンタム Top3 戦略で実際に選ばれた銘柄のログ取得と検証
"""
from __future__ import annotations
import sys, json, time, pickle, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
from _multipos_backtest import UNIVERSE_50

CACHE_PATH = Path("/Users/sanosano/projects/kimochi-max/results/_cache_alldata.pkl")
ITER43_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_rethink.json")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_hallucination.json")


def fetch_coingecko_daily(coin_id, from_ts, to_ts):
    """CoinGecko public API (no key required)"""
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
           f"?vs_currency=usd&from={from_ts}&to={to_ts}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        return data.get("prices", [])
    except Exception as e:
        print(f"   ⚠️  {coin_id}: {e}")
        return []


def get_price_on_date(prices_ts_list, target_date):
    """target_date に最も近い価格を取得"""
    target_ms = int(pd.Timestamp(target_date).timestamp() * 1000)
    if not prices_ts_list: return None
    closest = min(prices_ts_list, key=lambda x: abs(x[0] - target_ms))
    return closest[1]


def main():
    print("=" * 80)
    print("🔍 Iter43 ハルシネーション検証")
    print("=" * 80)

    # ━━━ Step 1: キャッシュデータの実在確認 ━━━
    print("\n📦 Step 1: キャッシュデータ確認")
    if not CACHE_PATH.exists():
        print("❌ キャッシュファイル無し")
        return
    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    print(f"   ✅ キャッシュ存在 (銘柄数: {len(all_data)})")
    print(f"   UNIVERSE_50 と一致: {len(set(all_data.keys()) & set(UNIVERSE_50))}/{len(UNIVERSE_50)}")

    btc_df = all_data["BTC/USDT"]
    eth_df = all_data["ETH/USDT"]
    print(f"   BTC/USDT データ期間: {btc_df.index.min()} 〜 {btc_df.index.max()}")
    print(f"   BTC/USDT データ点数: {len(btc_df)}")

    # ━━━ Step 2: CoinGecko 主要マイルストーン日で価格照合 ━━━
    print("\n🌐 Step 2: CoinGecko API と価格照合")

    milestones = {
        "2020-01-01": "年初",
        "2020-12-31": "2020年末",
        "2021-12-31": "2021年末",
        "2022-12-31": "2022年末",
        "2023-12-31": "2023年末",
        "2024-12-31": "2024年末",
    }

    # 2019-12-20 〜 2024-12-31 のデータ取得 (CoinGecko)
    from_ts = int(datetime(2019, 12, 20, tzinfo=timezone.utc).timestamp())
    to_ts = int(datetime(2024, 12, 31, tzinfo=timezone.utc).timestamp())

    comparisons = {}
    for coin_id, sym in [("bitcoin", "BTC/USDT"), ("ethereum", "ETH/USDT"),
                         ("solana", "SOL/USDT")]:
        print(f"\n   📡 {coin_id} 取得中...")
        prices = fetch_coingecko_daily(coin_id, from_ts, to_ts)
        if not prices:
            comparisons[sym] = {"error": "CoinGecko取得失敗"}
            continue
        time.sleep(3)  # API rate limit

        df = all_data.get(sym)
        if df is None:
            comparisons[sym] = {"error": f"{sym} がキャッシュに無い"}
            continue

        rows = []
        for date_str, label in milestones.items():
            dt = pd.Timestamp(date_str)
            cg_price = get_price_on_date(prices, date_str)
            # キャッシュ側: 最も近い日
            if len(df.index) == 0:
                continue
            closest_idx = df.index.get_indexer([dt], method="nearest")[0]
            if 0 <= closest_idx < len(df):
                cache_price = float(df.iloc[closest_idx]["close"])
                cache_date = str(df.index[closest_idx])[:10]
            else:
                cache_price = None; cache_date = None
            diff_pct = None
            if cg_price and cache_price:
                diff_pct = (cache_price - cg_price) / cg_price * 100
            rows.append({
                "milestone": date_str,
                "label": label,
                "binance_cache_date": cache_date,
                "binance_price": round(cache_price, 2) if cache_price else None,
                "coingecko_price": round(cg_price, 2) if cg_price else None,
                "diff_pct": round(diff_pct, 3) if diff_pct is not None else None,
            })
        comparisons[sym] = rows
        print(f"      {sym} 5マイルストーン取得完了")

    # ━━━ Step 3: 年別リターン独立計算 vs Iter43 R01/R02 ━━━
    print("\n📊 Step 3: 年別リターン独立計算と比較")
    iter43 = json.loads(ITER43_PATH.read_text())
    indep_yearly = {}
    for sym in ["BTC/USDT", "ETH/USDT"]:
        df = all_data[sym]
        yearly = {}
        prev_close = None
        for y in range(2020, 2025):
            year_df = df[df.index.year == y]
            if len(year_df) == 0: continue
            start = year_df["close"].iloc[0]
            end = year_df["close"].iloc[-1]
            base = prev_close if prev_close else start
            yearly[y] = round((end / base - 1) * 100, 2)
            prev_close = end
        indep_yearly[sym] = yearly
        print(f"   {sym} 独立計算: {yearly}")

    # Iter43 R01/R02 との比較
    iter43_r01 = iter43["results"].get("R01 BTC単純保有", {}).get("yearly", {})
    iter43_r02 = iter43["results"].get("R02 ETH単純保有", {}).get("yearly", {})

    print(f"\n   Iter43 R01 BTC: {iter43_r01}")
    print(f"   Iter43 R02 ETH: {iter43_r02}")

    r01_diff = {}
    for y in range(2020, 2025):
        ind = indep_yearly["BTC/USDT"].get(y, 0)
        r43 = iter43_r01.get(str(y), iter43_r01.get(y, 0))
        r01_diff[y] = round(r43 - ind, 2)
    print(f"\n   BTC差分 (R01 - 独立計算): {r01_diff}")

    # ━━━ Step 4: モメンタム Top3 戦略ログ取得 ━━━
    print("\n🎯 Step 4: モメンタム Top3 戦略の月次選択銘柄")
    print("   (過去90日リターンでトップ3銘柄を毎月選択)")

    momentum_log = []
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if pd.Timestamp("2020-01-01") <= d <= pd.Timestamp("2024-12-31")]

    last_month = None
    for date in dates:
        if last_month is not None and date.month == last_month: continue
        last_month = date.month

        # BTC レジームチェック
        btc_r = btc_df.loc[date]
        btc_price = btc_r["close"]; btc_ema200 = btc_r.get("ema200")
        regime = "現金" if (not pd.isna(btc_ema200) and btc_price < btc_ema200) else "投資"

        if regime == "投資":
            # 上位3銘柄を選択
            scores = []
            for sym, df in all_data.items():
                if date not in df.index: continue
                past_idx = df.index[(df.index < date) &
                                     (df.index >= date - pd.Timedelta(days=90))]
                if len(past_idx) < 20: continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past_idx[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20: continue
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)
            top3 = [{"sym": s, "ret_90d": round(r * 100, 1)} for s, r in scores[:3]]
        else:
            top3 = []

        momentum_log.append({
            "date": str(date)[:10],
            "regime": regime,
            "btc_price": round(float(btc_price), 2),
            "top3": top3,
        })

    print(f"   ログ件数: {len(momentum_log)}ヶ月")

    # 2021年 (+2483%) の選択銘柄を詳細表示
    print(f"\n   📌 2021年の月次選択銘柄（年+2483%の根拠）:")
    for log in momentum_log:
        if log["date"].startswith("2021"):
            tops = ", ".join([f"{t['sym']}(+{t['ret_90d']}%)" for t in log["top3"]])
            print(f"      {log['date']} [{log['regime']}] BTC=${log['btc_price']:,.0f}")
            if tops:
                print(f"         → {tops}")

    # ━━━ 結果まとめ ━━━
    out = {
        "step1_cache": {
            "n_symbols": len(all_data),
            "universe50_matched": len(set(all_data.keys()) & set(UNIVERSE_50)),
            "btc_data_period": [str(btc_df.index.min())[:10], str(btc_df.index.max())[:10]],
            "btc_data_points": len(btc_df),
        },
        "step2_coingecko_comparison": comparisons,
        "step3_yearly_verification": {
            "independent_btc": {y: indep_yearly["BTC/USDT"].get(y) for y in range(2020, 2025)},
            "independent_eth": {y: indep_yearly["ETH/USDT"].get(y) for y in range(2020, 2025)},
            "iter43_r01_btc": iter43_r01,
            "iter43_r02_eth": iter43_r02,
            "btc_diff_r01_vs_indep": r01_diff,
        },
        "step4_momentum_log": momentum_log,
    }

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
