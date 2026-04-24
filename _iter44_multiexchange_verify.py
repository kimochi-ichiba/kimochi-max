"""
Iter44 Step1: 複数取引所での価格相互検証
=============================================
検証対象: BTC, ETH, SOL, DOGE, AVAX の5銘柄
比較元:
  - Binanceキャッシュ (既存: _cache_alldata.pkl)
比較先:
  - MEXC  (api.mexc.com/api/v3/klines)
  - Bitget (api.bitget.com/api/v2/spot/market/candles)
  - yfinance (Yahoo Finance経由: BTC-USD 等)

マイルストーン日 (10点):
  2019-12-31, 2020-06-30, 2020-12-31, 2021-06-30, 2021-12-31,
  2022-06-30, 2022-12-31, 2023-06-30, 2023-12-31, 2024-06-30, 2024-12-30
"""
from __future__ import annotations
import sys, json, time, pickle, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter44_multiexchange.json")

SYMBOLS_TO_CHECK = [
    # (キャッシュのsymbol, Binance symbol, MEXC symbol, Bitget symbol, yfinance symbol)
    ("BTC/USDT", "BTCUSDT", "BTCUSDT", "BTCUSDT", "BTC-USD"),
    ("ETH/USDT", "ETHUSDT", "ETHUSDT", "ETHUSDT", "ETH-USD"),
    ("SOL/USDT", "SOLUSDT", "SOLUSDT", "SOLUSDT", "SOL-USD"),
    ("DOGE/USDT","DOGEUSDT","DOGEUSDT","DOGEUSDT","DOGE-USD"),
    ("AVAX/USDT","AVAXUSDT","AVAXUSDT","AVAXUSDT","AVAX-USD"),
]

MILESTONES = [
    "2019-12-31","2020-06-30","2020-12-31","2021-06-30","2021-12-31",
    "2022-06-30","2022-12-31","2023-06-30","2023-12-31","2024-06-28","2024-12-30",
]


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_mexc_daily(symbol, start_date, end_date):
    """MEXC Spot Kline API (公開、無認証)
       https://api.mexc.com/api/v3/klines?symbol=BTCUSDT&interval=1d&startTime=ms&endTime=ms
    """
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end_date).timestamp() * 1000)
    url = (f"https://api.mexc.com/api/v3/klines?symbol={symbol}"
           f"&interval=1d&startTime={start_ms}&endTime={end_ms}&limit=1000")
    try:
        data = http_get(url)
        # [openTime, open, high, low, close, volume, ...]
        return {int(k[0]): float(k[4]) for k in data}
    except Exception as e:
        return {"error": str(e)}


def fetch_bitget_daily(symbol, start_date, end_date):
    """Bitget Spot Candles API (公開、無認証)
       https://api.bitget.com/api/v2/spot/market/candles?symbol=BTCUSDT&granularity=1day
    """
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end_date).timestamp() * 1000)
    url = (f"https://api.bitget.com/api/v2/spot/market/candles?symbol={symbol}"
           f"&granularity=1day&startTime={start_ms}&endTime={end_ms}&limit=1000")
    try:
        resp = http_get(url)
        if resp.get("code") != "00000":
            return {"error": resp.get("msg", "Bitget error")}
        # [ts, open, high, low, close, baseVol, quoteVol, usdtVol]
        return {int(k[0]): float(k[4]) for k in resp.get("data", [])}
    except Exception as e:
        return {"error": str(e)}


def fetch_yfinance_daily(yf_symbol, start_date, end_date):
    """yfinance 経由で Yahoo Finance から取得"""
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance not installed"}
    try:
        data = yf.download(yf_symbol, start=start_date, end=end_date, progress=False, auto_adjust=False)
        if data.empty:
            return {"error": "empty data"}
        # 日付(date) -> close
        result = {}
        for idx, row in data.iterrows():
            date_str = str(idx)[:10]
            close = float(row["Close"].iloc[0] if hasattr(row["Close"], 'iloc') else row["Close"])
            result[date_str] = close
        return result
    except Exception as e:
        return {"error": str(e)}


def get_mexc_price_on(prices_ms_map, target_date):
    if isinstance(prices_ms_map, dict) and "error" in prices_ms_map:
        return None
    target_ms = int(pd.Timestamp(target_date).timestamp() * 1000)
    if not prices_ms_map: return None
    closest_ms = min(prices_ms_map.keys(), key=lambda x: abs(x - target_ms))
    if abs(closest_ms - target_ms) > 86400_000 * 3:  # 3日以上乖離は無効
        return None
    return prices_ms_map[closest_ms]


def get_yf_price_on(prices_str_map, target_date):
    if isinstance(prices_str_map, dict) and "error" in prices_str_map:
        return None
    if not prices_str_map: return None
    dates = sorted(prices_str_map.keys())
    # 最も近い日
    target = pd.Timestamp(target_date)
    closest = min(dates, key=lambda d: abs((pd.Timestamp(d) - target).days))
    if abs((pd.Timestamp(closest) - target).days) > 3: return None
    return prices_str_map[closest]


def main():
    print("=" * 90)
    print("🌐 Iter44 Step1: 複数取引所で価格相互検証")
    print("=" * 90)

    # キャッシュ読込
    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)

    # 期間: 2019-12-20 〜 2024-12-31
    start_date = "2019-12-20"
    end_date = "2024-12-31"

    results = {}
    for cache_sym, bin_sym, mexc_sym, bitget_sym, yf_sym in SYMBOLS_TO_CHECK:
        print(f"\n📊 {cache_sym} の検証中...")

        # キャッシュデータ
        cache_df = all_data.get(cache_sym)
        if cache_df is None:
            print(f"   ❌ キャッシュに {cache_sym} が無い")
            continue

        # MEXC (全期間一括でなく、年ごとに分割すると速い)
        print(f"   📡 MEXC取得中...")
        mexc_all = {}
        for y in range(2019, 2025):
            y_start = f"{y}-01-01" if y > 2019 else "2019-12-20"
            y_end = f"{y}-12-31"
            r = fetch_mexc_daily(mexc_sym, y_start, y_end)
            if isinstance(r, dict) and "error" in r:
                print(f"      ⚠️ {y}: {r['error']}")
            else:
                mexc_all.update(r)
            time.sleep(0.3)
        print(f"      MEXC: {len(mexc_all)}日分")

        # Bitget (同じく年分割)
        print(f"   📡 Bitget取得中...")
        bitget_all = {}
        for y in range(2019, 2025):
            y_start = f"{y}-01-01" if y > 2019 else "2019-12-20"
            y_end = f"{y}-12-31"
            r = fetch_bitget_daily(bitget_sym, y_start, y_end)
            if isinstance(r, dict) and "error" in r:
                print(f"      ⚠️ {y}: {r['error']}")
            else:
                bitget_all.update(r)
            time.sleep(0.3)
        print(f"      Bitget: {len(bitget_all)}日分")

        # yfinance
        print(f"   📡 yfinance取得中 ({yf_sym})...")
        yf_all = fetch_yfinance_daily(yf_sym, start_date, end_date)
        if isinstance(yf_all, dict) and "error" in yf_all:
            print(f"      ⚠️ {yf_all['error']}")
            yf_all = {}
        else:
            print(f"      yfinance: {len(yf_all)}日分")

        # マイルストーン毎に4者比較
        milestone_rows = []
        for date_str in MILESTONES:
            dt = pd.Timestamp(date_str)
            # Binance (キャッシュ)
            if len(cache_df.index) > 0:
                idx = cache_df.index.get_indexer([dt], method="nearest")[0]
                if 0 <= idx < len(cache_df):
                    binance_price = float(cache_df.iloc[idx]["close"])
                else:
                    binance_price = None
            else:
                binance_price = None

            mexc_price = get_mexc_price_on(mexc_all, date_str)
            bitget_price = get_mexc_price_on(bitget_all, date_str)  # 同じ形式
            yf_price = get_yf_price_on(yf_all, date_str)

            # 基準は binance_price。他との乖離率
            def diff(ref, other):
                if ref is None or other is None: return None
                return (other - ref) / ref * 100

            row = {
                "date": date_str,
                "binance": round(binance_price, 6) if binance_price else None,
                "mexc": round(mexc_price, 6) if mexc_price else None,
                "bitget": round(bitget_price, 6) if bitget_price else None,
                "yfinance": round(yf_price, 6) if yf_price else None,
                "mexc_vs_binance_pct": round(diff(binance_price, mexc_price), 3) if mexc_price else None,
                "bitget_vs_binance_pct": round(diff(binance_price, bitget_price), 3) if bitget_price else None,
                "yf_vs_binance_pct": round(diff(binance_price, yf_price), 3) if yf_price else None,
            }
            milestone_rows.append(row)
            # 整形表示
            b_str = f"${binance_price:,.2f}" if binance_price else "-"
            m_str = f"${mexc_price:,.2f}" if mexc_price else "-"
            bg_str = f"${bitget_price:,.2f}" if bitget_price else "-"
            y_str = f"${yf_price:,.2f}" if yf_price else "-"
            md = row["mexc_vs_binance_pct"]
            bgd = row["bitget_vs_binance_pct"]
            yd = row["yf_vs_binance_pct"]
            print(f"      {date_str}: Binance={b_str:>14s} / MEXC={m_str:>14s} ({md:+.2f}%) "
                  f"/ Bitget={bg_str:>14s} ({bgd:+.2f}%)" if md is not None and bgd is not None
                  else f"      {date_str}: Binance={b_str:>14s} MEXC={m_str}/Bitget={bg_str}")

        results[cache_sym] = milestone_rows

    # サマリー: 各乖離率の最大絶対値
    print(f"\n{'=' * 90}")
    print(f"📏 取引所間の最大乖離率まとめ")
    print(f"{'=' * 90}")
    summary = {}
    for sym, rows in results.items():
        mexc_diffs = [abs(r["mexc_vs_binance_pct"]) for r in rows
                      if r.get("mexc_vs_binance_pct") is not None]
        bitget_diffs = [abs(r["bitget_vs_binance_pct"]) for r in rows
                        if r.get("bitget_vs_binance_pct") is not None]
        yf_diffs = [abs(r["yf_vs_binance_pct"]) for r in rows
                    if r.get("yf_vs_binance_pct") is not None]
        summary[sym] = {
            "mexc_max_diff": round(max(mexc_diffs), 3) if mexc_diffs else None,
            "mexc_avg_diff": round(sum(mexc_diffs) / len(mexc_diffs), 3) if mexc_diffs else None,
            "bitget_max_diff": round(max(bitget_diffs), 3) if bitget_diffs else None,
            "bitget_avg_diff": round(sum(bitget_diffs) / len(bitget_diffs), 3) if bitget_diffs else None,
            "yf_max_diff": round(max(yf_diffs), 3) if yf_diffs else None,
            "yf_avg_diff": round(sum(yf_diffs) / len(yf_diffs), 3) if yf_diffs else None,
        }
        print(f"  {sym:10s}: MEXC max={summary[sym]['mexc_max_diff']}%  avg={summary[sym]['mexc_avg_diff']}% | "
              f"Bitget max={summary[sym]['bitget_max_diff']}%  avg={summary[sym]['bitget_avg_diff']}% | "
              f"yfinance max={summary[sym]['yf_max_diff']}%  avg={summary[sym]['yf_avg_diff']}%")

    out = {"milestones": MILESTONES, "comparisons": results, "summary": summary}
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
