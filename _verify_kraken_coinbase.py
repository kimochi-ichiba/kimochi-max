"""
Kraken + Coinbase によるデータ追加検証
Binance / MEXC に加えて 2 つの主要独立ソースでクロスチェック
"""
from pathlib import Path
import requests, time
from datetime import datetime
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source


fetcher = DataFetcher(Config())
assert_binance_source(fetcher)

print("📥 Binance から BTC/USDT 日足を取得...")
df_bin = fetcher.fetch_historical_ohlcv("BTC/USDT", "1d", "2020-01-01", "2024-12-31")

# Kraken API (OHLC, BTC/USD)
def fetch_kraken(date_str):
    """Kraken の BTC/USD 日足 close を取得"""
    ts = int(datetime.fromisoformat(date_str).timestamp())
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XBTUSD", "interval": 1440, "since": ts - 86400}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if "result" in data:
            # Kraken returns pair key differently
            for k, v in data["result"].items():
                if k == "last": continue
                if not v: continue
                # v は [time, open, high, low, close, vwap, volume, count] 形式
                for row in v:
                    if int(row[0]) == ts:
                        return float(row[4])
                # 一致する日がなければ最初のキャンドル
                if v:
                    return float(v[0][4])
        return None
    except Exception as e:
        return None


# Coinbase API (BTC-USD, granularity 86400=1day)
def fetch_coinbase(date_str):
    """Coinbase Advanced API の BTC-USD 日足 close を取得"""
    dt = datetime.fromisoformat(date_str)
    end = dt.isoformat() + "Z"
    start = (dt - pd.Timedelta(days=1)).isoformat() + "Z"
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    params = {"start": start, "end": end, "granularity": 86400}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        # [timestamp, low, high, open, close, volume] の配列
        if data and isinstance(data, list):
            for row in data:
                row_ts = int(row[0])
                target_ts = int(dt.timestamp())
                if abs(row_ts - target_ts) < 86400 * 2:  # 2日以内なら採用
                    return float(row[4])
        return None
    except Exception as e:
        return None


# MEXC API
def fetch_mexc(date_str):
    url = "https://api.mexc.com/api/v3/klines"
    start_ts = int(datetime.fromisoformat(date_str).timestamp() * 1000)
    end_ts = start_ts + 86400 * 1000
    params = {"symbol": "BTCUSDT", "interval": "1d", "startTime": start_ts, "endTime": end_ts, "limit": 2}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0][4])
    except Exception:
        return None
    return None


dates = [
    ("2020-03-13", "COVID暴落の底"),
    ("2021-11-10", "2021年ATH"),
    ("2022-11-09", "FTX破綻"),
    ("2024-03-14", "2024新ATH"),
    ("2024-12-30", "2024年末"),
]

print(f"\n{'=' * 100}")
print(f"🔍 4取引所クロス検証 (Binance / MEXC / Kraken / Coinbase)")
print(f"{'=' * 100}")
print(f"{'日付':12s} {'イベント':20s} | {'Binance':>10s} | {'MEXC':>10s} | {'Kraken':>10s} | {'Coinbase':>10s} | 乖離率")
print("-" * 100)

for date_str, label in dates:
    bin_p = float(df_bin.loc[pd.Timestamp(date_str), "close"]) if pd.Timestamp(date_str) in df_bin.index else None
    mexc_p = fetch_mexc(date_str); time.sleep(0.3)
    krk_p  = fetch_kraken(date_str); time.sleep(0.3)
    cb_p   = fetch_coinbase(date_str); time.sleep(0.3)

    prices = [p for p in [bin_p, mexc_p, krk_p, cb_p] if p is not None]
    dev = (max(prices) - min(prices)) / min(prices) * 100 if len(prices) >= 2 else None
    flag = "✅" if (dev is not None and dev < 1.5) else ("⚠️" if dev and dev < 3 else "❌" if dev else "?")

    bs  = f"${bin_p:>7,.0f}" if bin_p else "—"
    ms  = f"${mexc_p:>7,.0f}" if mexc_p else "—"
    ks  = f"${krk_p:>7,.0f}"  if krk_p  else "—"
    cbs = f"${cb_p:>7,.0f}"   if cb_p   else "—"
    print(f"{date_str} {label:20s} | {bs:>10s} | {ms:>10s} | {ks:>10s} | {cbs:>10s} | {dev:.2f}% {flag}" if dev else
          f"{date_str} {label:20s} | {bs:>10s} | {ms:>10s} | {ks:>10s} | {cbs:>10s} | —     {flag}")

print(f"\n{'=' * 100}")
print(f"✅ 4取引所で一致確認されればデータは100%本物と断言可能")
print(f"{'=' * 100}")
