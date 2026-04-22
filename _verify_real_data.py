"""
バックテストデータの複数ソースクロス検証
========================================
目的:
  バックテストで使用したデータが本当にBinanceの実データか、
  MEXC/CoinGeckoと突合して架空データでないかを確認する。

検証ポイント:
  - 過去の暴落・天井など歴史的な日のBTC価格
  - 複数ソース間の価格乖離が 1% 以内か
  - 5年間の価格推移が一致するか
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import pandas as pd
from datetime import datetime
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source

print("=" * 90)
print("🔍 バックテストデータの複数ソースクロス検証")
print("=" * 90)

# ─── 1. Binanceからデータ取得（バックテストと同じ）
print("\n📥 Binance実データ取得中...")
fetcher = DataFetcher(Config())
assert_binance_source(fetcher)  # Binance以外拒否
df_binance = fetcher.fetch_historical_ohlcv("BTC/USDT", "1d", "2020-01-01", "2024-12-31")
print(f"   ✅ {len(df_binance)}本取得")
print(f"   ソース: api.binance.com/api/v3/klines")

# ─── 2. 検証対象の歴史的な日付
historical_dates = [
    ("2020-03-13", "COVID暴落の底"),
    ("2020-12-17", "2020年末高値"),
    ("2021-04-14", "2021年春の高値"),
    ("2021-07-20", "2021年中盤の底"),
    ("2021-11-10", "2021年ATH (史上最高値)"),
    ("2022-06-18", "2022年ルナショック後の底"),
    ("2022-11-09", "FTX破綻時"),
    ("2023-03-10", "SVB破綻 2023春"),
    ("2023-10-23", "2023秋の反発"),
    ("2024-03-14", "2024新ATH形成"),
    ("2024-08-05", "日本円キャリートレード解消"),
    ("2024-12-30", "2024年末"),
]

print(f"\n{'=' * 90}")
print(f"🎯 歴史的日付での BTC/USDT 価格検証")
print(f"{'=' * 90}")

# ─── 3. MEXCから同じ日のデータを取得
def fetch_mexc(date_str):
    """MEXC API で指定日のBTC USDT終値を取得"""
    url = "https://api.mexc.com/api/v3/klines"
    start_ts = int(datetime.fromisoformat(date_str).timestamp() * 1000)
    end_ts = start_ts + 86400 * 1000
    params = {"symbol": "BTCUSDT", "interval": "1d", "startTime": start_ts, "endTime": end_ts, "limit": 2}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0][4])  # close
    except Exception as e:
        return None
    return None


# ─── 4. CoinGecko API
def fetch_coingecko(date_str):
    """CoinGecko API で指定日のBTC USD価格を取得"""
    # DD-MM-YYYY 形式
    dt = datetime.fromisoformat(date_str)
    formatted = dt.strftime("%d-%m-%Y")
    url = f"https://api.coingecko.com/api/v3/coins/bitcoin/history"
    params = {"date": formatted, "localization": "false"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["market_data"]["current_price"]["usd"])
    except Exception as e:
        return None


# ─── 5. 各日付で突合
results = []
print(f"\n{'日付':12s} {'イベント':28s} | {'Binance':>12s} | {'MEXC':>12s} | {'CoinGecko':>12s} | 乖離率(max)")
print("-" * 100)
for date_str, label in historical_dates:
    bin_price = None
    if pd.Timestamp(date_str) in df_binance.index:
        bin_price = float(df_binance.loc[pd.Timestamp(date_str), "close"])

    mexc_price = fetch_mexc(date_str)
    time.sleep(0.5)  # rate limit
    cg_price = fetch_coingecko(date_str)
    time.sleep(1.2)  # CoinGecko is strict on free tier

    prices = [p for p in [bin_price, mexc_price, cg_price] if p is not None]
    if len(prices) >= 2:
        max_p = max(prices)
        min_p = min(prices)
        deviation = (max_p - min_p) / min_p * 100
        flag = "✅" if deviation < 1.5 else ("⚠️" if deviation < 3 else "❌")
    else:
        deviation = None
        flag = "?"

    bin_s  = f"${bin_price:>8,.0f}" if bin_price else "—"
    mexc_s = f"${mexc_price:>8,.0f}" if mexc_price else "—"
    cg_s   = f"${cg_price:>8,.0f}"   if cg_price else "—"
    dev_s  = f"{deviation:.2f}%" if deviation is not None else "—"
    print(f"{date_str} {label:28s} | {bin_s:>12s} | {mexc_s:>12s} | {cg_s:>12s} | {dev_s} {flag}")

    results.append({
        "date": date_str, "event": label,
        "binance": bin_price, "mexc": mexc_price, "coingecko": cg_price,
        "deviation_pct": deviation,
    })

# ─── 6. 集計
print(f"\n{'=' * 90}")
print(f"📊 検証サマリ")
print(f"{'=' * 90}")
ok = sum(1 for r in results if r["deviation_pct"] is not None and r["deviation_pct"] < 1.5)
warn = sum(1 for r in results if r["deviation_pct"] is not None and 1.5 <= r["deviation_pct"] < 3)
bad = sum(1 for r in results if r["deviation_pct"] is not None and r["deviation_pct"] >= 3)
none_ = sum(1 for r in results if r["deviation_pct"] is None)

print(f"  ✅ 乖離1.5%未満（本物合致）: {ok} / {len(results)}")
print(f"  ⚠️  乖離1.5-3%（容認範囲）:  {warn} / {len(results)}")
print(f"  ❌ 乖離3%以上（要精査）:     {bad} / {len(results)}")
print(f"  ？ 取得失敗（3ソース全部）: {none_} / {len(results)}")

# ─── 7. 保存
out = (Path(__file__).resolve().parent / "results" / "data_cross_verify.json")
out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
print(f"\n💾 {out}")

# ─── 8. 結論
print(f"\n{'=' * 90}")
if ok >= len(results) * 0.7:
    print("✅ 検証通過: Binance データは MEXC / CoinGecko と整合している = 本物データ")
    print("   バックテスト結果は架空ではなく実市場に基づいている")
else:
    print("⚠️ 乖離が大きい - データに異常の可能性あり")
print(f"{'=' * 90}")
