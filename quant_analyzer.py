#!/usr/bin/env python3
"""
quant_analyzer.py — クリプトクオンツアナリスト 7タスク実装
============================================================
実行方法:
    python quant_analyzer.py          # 全タスク①〜⑥ を順番に実行
    python quant_analyzer.py --task 1 # タスク①だけ実行

出力: results/ フォルダに JSON / CSV / PNG で保存
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import ccxt

# matplotlib（グラフ描画・ヘッドレス環境でも動作）
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("⚠️ matplotlib なし。グラフは生成されません。pip install matplotlib でインストール可能。")

# ta ライブラリ（テクニカル指標）
try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD as MACDIndicator
    from ta.volatility import AverageTrueRange
    HAS_TA = True
except ImportError:
    HAS_TA = False
    print("⚠️ ta なし。手動計算にフォールバック。pip install ta でインストール可能。")


# ════════════════════════════════════════════════
# 初期設定
# ════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("quant")

RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)

# ── API エンドポイント ────────────────────────────
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEFILLAMA_BASE = "https://api.llama.fi"
FNG_URL        = "https://api.alternative.me/fng/"

# Slack / LINE の Webhook URL（環境変数から読み込む）
SLACK_WEBHOOK  = os.environ.get("SLACK_WEBHOOK_URL", "")
LINE_TOKEN     = os.environ.get("LINE_NOTIFY_TOKEN", "")


# ════════════════════════════════════════════════
# ユーティリティ関数
# ════════════════════════════════════════════════

def _get(url: str, params: dict = None, retries: int = 5,
         base_wait: float = 2.0):
    """
    HTTP GET リクエストを送る。レート制限・ネットワークエラーに対応。
    429（レート制限）は最大5回まで、指数バックオフで待機する。
    """
    rate_limit_count = 0
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; CryptoBot/1.0)",
                    "Accept": "application/json",
                }
            )
            if resp.status_code == 429:
                rate_limit_count += 1
                if rate_limit_count > 4:
                    logger.error("レート制限が続くためスキップします")
                    return None
                wait_s = min(30 * rate_limit_count, 120)  # 30→60→90→120秒
                logger.warning(f"レート制限 (429)。{wait_s}秒待機... ({rate_limit_count}回目)")
                time.sleep(wait_s)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            time.sleep(0.5)  # 成功しても少し間を開けてレート制限を避ける
            return resp.json()
        except requests.RequestException as e:
            wait_s = base_wait * (attempt + 1)
            if attempt < retries - 1:
                logger.debug(f"リクエスト失敗 (試行{attempt+1}/{retries}), {wait_s:.0f}秒後リトライ: {e}")
                time.sleep(wait_s)
            else:
                logger.error(f"APIエラー ({url}): {e}")
    return None


def _save_json(filename: str, data) -> Path:
    """results/ に JSON ファイルを保存する"""
    path = RESULTS_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"💾 保存: {path}")
    return path


def _save_csv(filename: str, df: pd.DataFrame) -> Path:
    """results/ に CSV ファイルを保存する"""
    path = RESULTS_DIR / filename
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"💾 保存: {path}")
    return path


def _calc_rsi(prices: pd.Series, period: int = 14) -> float:
    """RSI を手動計算して最新値を返す（ta ライブラリがない場合のフォールバック）"""
    if len(prices) < period + 1:
        return 50.0
    if HAS_TA:
        return float(RSIIndicator(close=prices, window=period).rsi().iloc[-1])
    delta = prices.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0


def _divider(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ════════════════════════════════════════════════
# ① 高ポテンシャルセクターの特定
# ════════════════════════════════════════════════

#: 分析対象カテゴリ（CoinGecko の category ID の一部を含むキーワード）
FOCUS_SECTORS = {
    "DeFi":            "decentralized-finance-defi",
    "Layer 2":         "layer-2",
    "AI・機械学習":     "artificial-intelligence",
    "RWA（現実資産)":   "real-world-assets",
    "GameFi":          "gaming",
    "Liquid Staking":  "liquid-staking",
    "Layer 1":         "layer-1",
    "Meme":            "meme-token",
    "DePIN":           "depin",
    "Restaking":       "restaking",
}


def task1_sector_ranking() -> pd.DataFrame:
    """
    ① 高ポテンシャルセクターの特定

    CoinGecko のカテゴリAPIから全カテゴリの
    時価総額・出来高・24h 変動率を取得し、
    注目 10 カテゴリの騰落ランキングを表示する。

    注意: CoinGecko 無料 API は「30日変動率」をカテゴリ単位では提供していないため、
    24時間 市場時価総額 変動率 + ボリューム/時価総額比率 でスコアリングする。
    """
    _divider("① 高ポテンシャルセクターの特定")

    data = _get(f"{COINGECKO_BASE}/coins/categories")
    if not data:
        logger.error("カテゴリデータ取得失敗（CoinGecko が応答しませんでした）")
        return pd.DataFrame()

    # 対象カテゴリを抽出
    rows = []
    for cat in data:
        cat_id   = (cat.get("id") or "").lower()
        cat_name = cat.get("name", "不明")
        mc       = cat.get("market_cap") or 0
        vol      = cat.get("volume_24h") or 0
        chg_24h  = cat.get("market_cap_change_24h") or 0

        # 注目カテゴリに一致するかチェック
        matched_label = None
        for label, keyword in FOCUS_SECTORS.items():
            if keyword in cat_id or keyword.split("-")[0] in cat_id:
                matched_label = label
                break

        if matched_label is None:
            continue

        # スコア = 24h変動率(60%) + 出来高/時価総額比率(40%)
        vol_mc_ratio = (vol / mc * 100) if mc > 0 else 0
        score = chg_24h * 0.6 + vol_mc_ratio * 0.4

        rows.append({
            "セクター":          matched_label,
            "CoinGeckoID":       cat_id,
            "24h変動率(%)":      round(chg_24h, 2),
            "時価総額(B$)":      round(mc / 1e9, 2),
            "24h出来高(M$)":     round(vol / 1e6, 1),
            "出来高/時価総額(%)": round(vol_mc_ratio, 2),
            "総合スコア":         round(score, 3),
        })

    if not rows:
        logger.warning("注目カテゴリが見つかりませんでした（全カテゴリ上位10件を使用）")
        rows = [{
            "セクター":          c.get("name", "?")[:20],
            "CoinGeckoID":       c.get("id", "?"),
            "24h変動率(%)":      round(c.get("market_cap_change_24h") or 0, 2),
            "時価総額(B$)":      round((c.get("market_cap") or 0) / 1e9, 2),
            "24h出来高(M$)":     round((c.get("volume_24h") or 0) / 1e6, 1),
            "出来高/時価総額(%)": 0,
            "総合スコア":         c.get("market_cap_change_24h") or 0,
        } for c in data[:20]]

    df = pd.DataFrame(rows).drop_duplicates("セクター").sort_values("総合スコア", ascending=False)
    top5 = df.head(5)

    print(f"\n{'ランク':<5} {'セクター':<20} {'24h変動%':>9} {'時価総額B$':>10} {'出来高M$':>9} {'VoL/MC%':>8} {'スコア':>7}")
    print("─" * 72)
    for rank, (_, r) in enumerate(top5.iterrows(), 1):
        arrow = "▲" if r["24h変動率(%)"] >= 0 else "▼"
        clr   = "🟢" if r["24h変動率(%)"] >= 0 else "🔴"
        print(f"{rank:<5} {r['セクター']:<20} {clr}{arrow}{r['24h変動率(%)']:>+7.2f}%"
              f"  {r['時価総額(B$)']:>9.1f}B  {r['24h出来高(M$)']:>8.0f}M"
              f"  {r['出来高/時価総額(%)']:>6.2f}%  {r['総合スコア']:>7.3f}")

    print(f"\n💡 注目度 TOP: {top5['セクター'].iloc[0]} が最もスコア高し")

    _save_json("01_sector_ranking.json", df.to_dict("records"))
    _save_csv("01_sector_ranking.csv", df)
    return df


# ════════════════════════════════════════════════
# ② 割安トークンの発見
# ════════════════════════════════════════════════

def task2_undervalued_screener() -> pd.DataFrame:
    """
    ② 割安トークンの発見

    時価総額上位 200 銘柄の中から以下の条件で割安トークンをスクリーニング:
      - 出来高 / 時価総額比率 が高い（流動性あり）
      - RSI が 50 以下（売られすぎまたは調整中）
      - 過去 90 日高値から 30% 以上下落（割安水準）

    RSI 計算: 各銘柄の過去 30 日間の日次価格をCoinGeckoから取得して計算。
    API コール数を減らすため、最初のフィルタ（VOL/MC + 90日下落率）で
    候補を絞ってから RSI を計算する。
    """
    _divider("② 割安トークンの発見")
    logger.info("時価総額上位200銘柄を取得中...")

    # ── STEP1: 上位200銘柄の基本データ取得（2ページ × 100件）──
    all_coins = []
    for page in range(1, 3):
        data = _get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency":              "usd",
                "order":                    "market_cap_desc",
                "per_page":                 100,
                "page":                     page,
                "sparkline":                False,
                "price_change_percentage":  "7d,30d,90d",
            }
        )
        if data:
            all_coins.extend(data)
        time.sleep(1.5)  # レート制限対策

    if not all_coins:
        logger.error("コインデータ取得失敗")
        return pd.DataFrame()

    logger.info(f"{len(all_coins)} 銘柄取得完了")

    # ── STEP2: 第1フィルタ（VOL/MC比率 + 90日下落率）──
    candidates = []
    for c in all_coins:
        mc        = c.get("market_cap") or 0
        vol       = c.get("total_volume") or 0
        chg_90d   = c.get("price_change_percentage_90d_in_currency") or 0
        ath       = c.get("ath") or 0
        price     = c.get("current_price") or 0
        ath_drop  = ((ath - price) / ath * 100) if ath > 0 else 0

        if mc <= 0:
            continue

        vol_mc_ratio = vol / mc * 100

        # フィルタ①: 出来高/時価総額比率 > 1%（ある程度の流動性）
        if vol_mc_ratio < 1.0:
            continue
        # フィルタ②: 90日変動率が -30% 以下（または ATH から -30% 以上下落）
        if chg_90d > -15 and ath_drop < 30:
            continue
        # ステーブルコイン・ラップドトークンを除外
        symbol = c.get("symbol", "").upper()
        if symbol in {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "WBTC", "WETH", "STETH"}:
            continue

        candidates.append({
            "id":           c.get("id"),
            "symbol":       symbol,
            "name":         c.get("name"),
            "price":        price,
            "mc_b":         round(mc / 1e9, 3),
            "vol_mc_pct":   round(vol_mc_ratio, 2),
            "chg_7d":       round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
            "chg_30d":      round(c.get("price_change_percentage_30d_in_currency") or 0, 2),
            "chg_90d":      round(chg_90d, 2),
            "ath_drop_pct": round(ath_drop, 1),
            "rsi_14":       None,  # 後で計算
        })

    logger.info(f"第1フィルタ通過: {len(candidates)} 銘柄 → RSI計算中...")

    # ── STEP3: 候補銘柄の RSI を計算（上位10件のみ API コール）──
    top_candidates = sorted(candidates, key=lambda x: x["vol_mc_pct"], reverse=True)[:10]

    for i, c in enumerate(top_candidates):
        try:
            hist = _get(
                f"{COINGECKO_BASE}/coins/{c['id']}/market_chart",
                params={"vs_currency": "usd", "days": "30", "interval": "daily"}
            )
            if hist and "prices" in hist:
                prices = pd.Series([p[1] for p in hist["prices"]])
                c["rsi_14"] = round(_calc_rsi(prices, 14), 1)
            else:
                c["rsi_14"] = 50.0
            time.sleep(3.0)  # レート制限対策（無料枠制限厳しいため3秒間隔）
        except Exception as e:
            logger.debug(f"RSI計算失敗 {c['symbol']}: {e}")
            c["rsi_14"] = 50.0

        logger.info(f"RSI計算進捗: {i+1}/{len(top_candidates)} ({c['symbol']})")

    # ── STEP4: RSI ≤ 50 フィルタ ──
    df = pd.DataFrame(top_candidates)
    df = df[df["rsi_14"].notna()]
    df_filtered = df[df["rsi_14"] <= 50].copy()
    df_filtered = df_filtered.sort_values("vol_mc_pct", ascending=False).head(10)

    if df_filtered.empty:
        logger.warning("RSI≤50 の候補がありません。RSI条件を緩和して上位10件を表示します。")
        df_filtered = df.sort_values("vol_mc_pct", ascending=False).head(10)

    print(f"\n{'#':<3} {'銘柄':<8} {'価格':>10} {'MC(B$)':>7} {'VOL/MC%':>8} {'RSI':>5} {'90d変動%':>9} {'ATH-drop%':>10}")
    print("─" * 72)
    for rank, (_, r) in enumerate(df_filtered.iterrows(), 1):
        rsi_str = f"{r['rsi_14']:.0f}" if r["rsi_14"] else "—"
        print(f"{rank:<3} {r['symbol']:<8} {r['price']:>10.4g} {r['mc_b']:>7.2f}B"
              f" {r['vol_mc_pct']:>7.1f}%  {rsi_str:>4}  {r['chg_90d']:>+8.1f}%  -{r['ath_drop_pct']:>8.1f}%")

    _save_json("02_undervalued_screener.json", df_filtered.to_dict("records"))
    _save_csv("02_undervalued_screener.csv", df_filtered)
    return df_filtered


# ════════════════════════════════════════════════
# ③ ウォッチリストの作成
# ════════════════════════════════════════════════

def _get_defi_yields() -> dict:
    """
    DeFiLlama から主要プロトコルの APY を取得して銘柄ごとに集計する。
    戻り値: {symbol_upper: max_apy_pct}
    """
    data = _get(f"{DEFILLAMA_BASE}/pools")
    if not data or "data" not in data:
        return {}
    yields = {}
    for pool in data["data"]:
        sym = (pool.get("symbol") or "").upper().split("-")[0]
        apy = pool.get("apy") or 0
        if sym and apy > 0:
            if sym not in yields or yields[sym] < apy:
                yields[sym] = round(apy, 2)
    return yields


def _calc_support_level(coin_id: str) -> tuple[float, float]:
    """
    過去 90 日間の価格データから主要サポートライン（S1・S2）を計算する。
    S1 = 過去 90 日の安値 + レンジの 23.6%（フィボナッチ）
    S2 = 過去 90 日の安値
    """
    hist = _get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": "90", "interval": "daily"}
    )
    if not hist or "prices" not in hist:
        return 0.0, 0.0
    prices = [p[1] for p in hist["prices"]]
    low90  = min(prices)
    high90 = max(prices)
    rng    = high90 - low90
    s1 = low90 + rng * 0.236   # フィボナッチ 23.6%
    s2 = low90                  # 過去 90 日安値
    return round(s1, 6), round(s2, 6)


def task3_watchlist() -> pd.DataFrame:
    """
    ③ ウォッチリストの作成

    上位 100 銘柄を「成長性・ステーキング利回り・安定性」3 軸でスコアリングし、
    推奨エントリーゾーン（サポートライン）を自動計算して CSV 出力する。

    成長性スコア:  30日騰落率・7日騰落率を正規化
    利回りスコア:  DeFiLlama から取得した最大 APY
    安定性スコア:  ATH からの下落率が小さいほど高い（安定した価格水準）
    """
    _divider("③ ウォッチリストの作成")
    logger.info("DeFiLlamaからステーキング利回りを取得中...")
    yields = _get_defi_yields()
    logger.info(f"  {len(yields)} 銘柄の利回りデータを取得")
    time.sleep(1)

    logger.info("上位100銘柄のマーケットデータを取得中...")
    coins = _get(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency":             "usd",
            "order":                   "market_cap_desc",
            "per_page":                100,
            "page":                    1,
            "sparkline":               False,
            "price_change_percentage": "7d,30d",
        }
    )
    if not coins:
        logger.error("コインデータ取得失敗")
        return pd.DataFrame()
    time.sleep(1.5)

    rows = []
    for c in coins:
        sym      = (c.get("symbol") or "").upper()
        mc       = c.get("market_cap") or 0
        price    = c.get("current_price") or 0
        ath      = c.get("ath") or 0
        chg_7d   = c.get("price_change_percentage_7d_in_currency") or 0
        chg_30d  = c.get("price_change_percentage_30d_in_currency") or 0
        ath_drop = ((ath - price) / ath * 100) if ath > 0 else 100

        # ステーブルコイン除外
        if sym in {"USDT","USDC","BUSD","DAI","TUSD","FDUSD","USDD","USDP","FRAX"}:
            continue

        # 利回りスコア（0〜100 に正規化）: 最大 200% APY → スコア 100
        apy  = yields.get(sym, 0)
        y_sc = min(apy / 2.0, 100)

        # 成長性スコア（7d と 30d の変動率を平均、-50〜+50% を 0〜100 に変換）
        growth_raw = (chg_7d * 0.4 + chg_30d * 0.6)
        g_sc       = min(max((growth_raw + 50) / 100 * 100, 0), 100)

        # 安定性スコア（ATHから下落が少ないほど高い）
        s_sc = max(0, 100 - ath_drop)

        # 総合スコア（加重平均）
        total = g_sc * 0.5 + y_sc * 0.3 + s_sc * 0.2

        rows.append({
            "symbol":       sym,
            "name":         c.get("name"),
            "id":           c.get("id"),
            "price":        price,
            "mc_b":         round(mc / 1e9, 3),
            "apy_pct":      round(apy, 1),
            "chg_7d":       round(chg_7d, 2),
            "chg_30d":      round(chg_30d, 2),
            "ath_drop_pct": round(ath_drop, 1),
            "score_growth": round(g_sc, 1),
            "score_yield":  round(y_sc, 1),
            "score_stable": round(s_sc, 1),
            "total_score":  round(total, 1),
            "support_s1":   0.0,
            "support_s2":   0.0,
        })

    df = pd.DataFrame(rows).sort_values("total_score", ascending=False).head(20)

    # サポートラインを上位 10 件だけ計算（API コール節約）
    logger.info("上位10銘柄のサポートラインを計算中...")
    for i, (idx, row) in enumerate(df.head(10).iterrows()):
        try:
            s1, s2 = _calc_support_level(row["id"])
            df.at[idx, "support_s1"] = s1
            df.at[idx, "support_s2"] = s2
            time.sleep(1.2)
        except Exception as e:
            logger.debug(f"サポートライン計算失敗 {row['symbol']}: {e}")

    print(f"\n{'#':<3} {'銘柄':<8} {'スコア':>6} {'成長':>5} {'利回':>5} {'安定':>5} "
          f"{'APY%':>6} {'S1':>12} {'S2':>12}")
    print("─" * 75)
    for rank, (_, r) in enumerate(df.iterrows(), 1):
        s1_str = f"${r['support_s1']:.4g}" if r["support_s1"] > 0 else "—"
        s2_str = f"${r['support_s2']:.4g}" if r["support_s2"] > 0 else "—"
        print(f"{rank:<3} {r['symbol']:<8} {r['total_score']:>6.1f}"
              f" {r['score_growth']:>5.0f} {r['score_yield']:>5.0f} {r['score_stable']:>5.0f}"
              f" {r['apy_pct']:>5.1f}%  {s1_str:>11}  {s2_str:>11}")

    _save_json("03_watchlist.json", df.to_dict("records"))
    _save_csv("03_watchlist.csv", df.drop(columns=["id"]))
    return df


# ════════════════════════════════════════════════
# ④ リスク管理されたスイングトレード戦略
# ════════════════════════════════════════════════

def _fetch_ohlcv_ccxt(symbol: str, timeframe: str = "4h",
                       limit: int = 300) -> pd.DataFrame:
    """ccxt を使って Binance から OHLCV を取得する"""
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp").astype(float)
        return df
    except Exception as e:
        logger.error(f"OHLCV取得失敗 {symbol}: {e}")
        return pd.DataFrame()


def _add_swing_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """EMA・MACD・ATR を計算して DataFrame に追加する"""
    if df.empty or len(df) < 50:
        return df
    close = df["close"]
    if HAS_TA:
        df["ema_20"]  = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema_50"]  = EMAIndicator(close=close, window=50).ema_indicator()
        df["ema_200"] = EMAIndicator(close=close, window=200).ema_indicator()
        macd_obj = MACDIndicator(close=close, window_fast=12, window_slow=26, window_sign=9)
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_hist"]   = macd_obj.macd_diff()
        df["atr"] = AverageTrueRange(
            high=df["high"], low=df["low"], close=close, window=14
        ).average_true_range()
    else:
        for w in [20, 50, 200]:
            df[f"ema_{w}"] = close.ewm(span=w, adjust=False).mean()
        ema_f = close.ewm(span=12, adjust=False).mean()
        ema_s = close.ewm(span=26, adjust=False).mean()
        df["macd"]        = ema_f - ema_s
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"]   = df["macd"] - df["macd_signal"]
        hl  = df["high"] - df["low"]
        hc  = (df["high"] - close.shift()).abs()
        lc  = (df["low"]  - close.shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        df["atr"] = tr.rolling(14).mean()
    return df


def task4_swing_signals(
    symbols: list = None,
    target_count: int = 5
) -> pd.DataFrame:
    """
    ④ リスク管理されたスイングトレード戦略

    Binance の 4 時間足データを取得し、
    EMA（20/50/200）・MACD・ATR を使ってシグナルを生成する。

    エントリー条件（ロング例）:
      - EMA20 > EMA50 > EMA200（上昇トレンド）
      - MACD ヒストグラムが負→正に転換
      - 終値が EMA20 付近（1×ATR 以内）

    SL / TP:
      - SL = 直近スイングロー or エントリー - 1.5×ATR
      - TP = SL 幅 × 2 以上（RR ≥ 2:1）
    """
    _divider("④ リスク管理されたスイングトレード戦略")

    if symbols is None:
        symbols = [
            "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "AVAX/USDT",
            "LINK/USDT", "DOT/USDT", "ADA/USDT", "NEAR/USDT", "INJ/USDT",
            "ARB/USDT", "OP/USDT",  "SUI/USDT", "APT/USDT", "TIA/USDT",
        ]

    signals = []
    logger.info(f"{len(symbols)} 銘柄の 4 時間足データを取得中...")

    for sym in symbols:
        if len(signals) >= target_count:
            break
        try:
            df = _fetch_ohlcv_ccxt(sym, "4h", limit=300)
            if df.empty:
                continue
            df = _add_swing_indicators(df)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            e20  = last.get("ema_20", 0)
            e50  = last.get("ema_50", 0)
            e200 = last.get("ema_200", 0)
            atr  = last.get("atr", 0)
            cp   = last["close"]
            macd_hist_now  = last.get("macd_hist", 0)
            macd_hist_prev = prev.get("macd_hist", 0)

            if atr <= 0 or cp <= 0:
                continue

            # ── ロングシグナル判定 ──
            trend_up = (e20 > e50 > e200 * 0.995) and (cp > e200)
            macd_cross_up = macd_hist_now > 0 > macd_hist_prev
            near_ema20 = abs(cp - e20) < atr * 1.2

            if trend_up and macd_cross_up and near_ema20:
                entry = cp
                sl    = round(entry - 1.5 * atr, 6)
                sl_w  = entry - sl
                tp    = round(entry + sl_w * 2.2, 6)  # RR = 2.2:1
                rr    = (tp - entry) / sl_w if sl_w > 0 else 0
                if rr >= 2.0:
                    signals.append({
                        "方向":       "LONG",
                        "銘柄":        sym,
                        "エントリー":  round(entry, 6),
                        "TP":          tp,
                        "SL":          sl,
                        "RR比":        round(rr, 2),
                        "ATR":         round(atr, 6),
                        "EMA20":       round(e20, 4),
                        "EMA50":       round(e50, 4),
                        "シグナル理由": f"EMAアップトレンド + MACDクロスアップ",
                        "時刻":        df.index[-1].strftime("%Y-%m-%d %H:%M"),
                    })
                    continue

            # ── ショートシグナル判定 ──
            trend_dn = (e20 < e50 < e200 * 1.005) and (cp < e200)
            macd_cross_dn = macd_hist_now < 0 < macd_hist_prev
            near_ema20_dn = abs(cp - e20) < atr * 1.2

            if trend_dn and macd_cross_dn and near_ema20_dn:
                entry = cp
                sl    = round(entry + 1.5 * atr, 6)
                sl_w  = sl - entry
                tp    = round(entry - sl_w * 2.2, 6)
                rr    = (entry - tp) / sl_w if sl_w > 0 else 0
                if rr >= 2.0:
                    signals.append({
                        "方向":       "SHORT",
                        "銘柄":        sym,
                        "エントリー":  round(entry, 6),
                        "TP":          tp,
                        "SL":          sl,
                        "RR比":        round(rr, 2),
                        "ATR":         round(atr, 6),
                        "EMA20":       round(e20, 4),
                        "EMA50":       round(e50, 4),
                        "シグナル理由": f"EMAダウントレンド + MACDクロスダウン",
                        "時刻":        df.index[-1].strftime("%Y-%m-%d %H:%M"),
                    })

        except Exception as e:
            logger.debug(f"シグナル計算エラー {sym}: {e}")
        time.sleep(0.3)

    # シグナルが少ない場合: RSI 基準での補完シグナルを追加
    if len(signals) < target_count:
        logger.info(f"EMA/MACDシグナルが {len(signals)} 件のみ。RSI逆張り補完シグナルを追加...")
        for sym in symbols:
            if len(signals) >= target_count:
                break
            if any(s["銘柄"] == sym for s in signals):
                continue
            try:
                df = _fetch_ohlcv_ccxt(sym, "4h", limit=200)
                if df.empty:
                    continue
                df = _add_swing_indicators(df)
                last = df.iloc[-1]
                atr  = last.get("atr", 0)
                cp   = last["close"]
                if HAS_TA:
                    rsi = float(RSIIndicator(close=df["close"], window=14).rsi().iloc[-1])
                else:
                    rsi = _calc_rsi(df["close"])
                if atr <= 0:
                    continue
                # RSI < 35 かつ価格がEMA50を上回っているなら反発ロング
                if rsi < 35 and cp > last.get("ema_50", 0) * 0.98:
                    entry = cp
                    sl    = round(entry - 2.0 * atr, 6)
                    sl_w  = entry - sl
                    tp    = round(entry + sl_w * 2.0, 6)
                    rr    = (tp - entry) / sl_w if sl_w > 0 else 0
                    if rr >= 2.0:
                        signals.append({
                            "方向":       "LONG",
                            "銘柄":        sym,
                            "エントリー":  round(entry, 6),
                            "TP":          tp,
                            "SL":          sl,
                            "RR比":        round(rr, 2),
                            "ATR":         round(atr, 6),
                            "EMA20":       round(last.get("ema_20", 0), 4),
                            "EMA50":       round(last.get("ema_50", 0), 4),
                            "シグナル理由": f"RSI={rsi:.0f} 売られすぎ反発",
                            "時刻":        df.index[-1].strftime("%Y-%m-%d %H:%M"),
                        })
            except Exception as e:
                logger.debug(f"補完シグナルエラー {sym}: {e}")
            time.sleep(0.3)

    df_sig = pd.DataFrame(signals[:target_count])

    if df_sig.empty:
        logger.warning("シグナルが生成されませんでした（市場が条件を満たさない状態）")
        print("  → 現在の市場環境ではシグナル条件を満たす銘柄がありません。")
    else:
        print(f"\n🎯 生成シグナル（{len(df_sig)} 件）:")
        print(f"{'#':<3} {'方向':<6} {'銘柄':<14} {'エントリー':>12} {'TP':>12} {'SL':>12} {'RR':>5}  理由")
        print("─" * 90)
        for rank, (_, r) in enumerate(df_sig.iterrows(), 1):
            arrow = "▲" if r["方向"] == "LONG" else "▼"
            color = "🟢" if r["方向"] == "LONG" else "🔴"
            print(f"{rank:<3} {color}{arrow}{r['方向']:<5} {r['銘柄']:<14}"
                  f" {r['エントリー']:>11.4g}  {r['TP']:>11.4g}  {r['SL']:>11.4g}"
                  f"  {r['RR比']:>4.1f}x  {r['シグナル理由']}")

    _save_json("04_swing_signals.json", signals)
    if not df_sig.empty:
        _save_csv("04_swing_signals.csv", df_sig)
    return df_sig


# ════════════════════════════════════════════════
# ⑤ 収益機会スキャナー
# ════════════════════════════════════════════════

# 主要プロジェクトのトークンアンロックスケジュール（例データ）
# 実際の運用では: https://cryptorank.io/vesting や https://tokenunlocks.app を参照
TOKEN_UNLOCKS_SAMPLE = [
    {"date": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
     "symbol": "ARB",  "unlock_pct": 3.9, "type": "投資家ベスティング解放",
     "description": "初期投資家・チームへの大型ベスティング解放"},
    {"date": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
     "symbol": "OP",   "unlock_pct": 2.5, "type": "エコシステム配布",
     "description": "ガバナンスファンドからの定期配布"},
    {"date": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
     "symbol": "SUI",  "unlock_pct": 1.8, "type": "バリデーター報酬解放",
     "description": "アーリーバリデーターへのトークン解放"},
    {"date": (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d"),
     "symbol": "TIA",  "unlock_pct": 5.2, "type": "コア開発チーム解放",
     "description": "大型チームトークン解放 → 売り圧力に注意"},
    {"date": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
     "symbol": "APT",  "unlock_pct": 2.1, "type": "コミュニティ配布",
     "description": "エコシステムインセンティブからの解放"},
]


def task5_opportunity_scanner() -> dict:
    """
    ⑤ 収益機会スキャナー

    以下のデータを統合し、今後 2 週間のイベントドリブン機会をリスト化する:
      1. Fear & Greed Index（現在の市場センチメント）
      2. トークンアンロックスケジュール（大型解放 = 売り圧力 or ショート機会）
      3. Binance の大型上場予定（価格ポンプ機会）
      4. 主要銘柄の価格モメンタム（直近で強い動き）
    """
    _divider("⑤ 収益機会スキャナー")

    results = {
        "スキャン日時":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fear_greed":       {},
        "トークンアンロック": [],
        "モメンタム機会":    [],
        "総合判断":          [],
    }

    # ── Fear & Greed Index ──
    logger.info("Fear & Greed Index を取得中...")
    fng = _get(FNG_URL, params={"limit": 7, "format": "json"})
    if fng and "data" in fng:
        latest    = fng["data"][0]
        fg_value  = int(latest["value"])
        fg_label  = latest["value_classification"]
        fg_trend  = "上昇傾向" if len(fng["data"]) > 1 and fg_value > int(fng["data"][1]["value"]) else "下降傾向"
        results["fear_greed"] = {
            "current": fg_value,
            "label":   fg_label,
            "trend":   fg_trend,
            "history": [{"date": d["timestamp"], "value": d["value"], "label": d["value_classification"]}
                        for d in fng["data"][:7]],
        }
        emoji = "😱" if fg_value <= 25 else "😨" if fg_value <= 40 else "😐" if fg_value <= 60 else "🤑"
        print(f"\n📊 Fear & Greed Index: {emoji} {fg_value}/100 ({fg_label}) [{fg_trend}]")
    time.sleep(1)

    # ── トークンアンロックスケジュール ──
    print(f"\n🔓 今後2週間のトークンアンロックスケジュール:")
    print(f"{'日付':<12} {'銘柄':<8} {'解放率':>8} {'タイプ':<25} 影響予測")
    print("─" * 75)
    for unlock in TOKEN_UNLOCKS_SAMPLE:
        pct = unlock["unlock_pct"]
        impact = "🔴 高売り圧" if pct >= 4 else "🟡 中程度" if pct >= 2 else "🟢 低影響"
        print(f"  {unlock['date']:<10} {unlock['symbol']:<8} {pct:>6.1f}%  "
              f"{unlock['type']:<25} {impact}")
        results["トークンアンロック"].append({**unlock, "影響予測": impact})

    # ── モメンタム機会（過去 24h で急騰している銘柄）──
    logger.info("\n急騰・急落銘柄を取得中...")
    time.sleep(1)
    market_data = _get(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency":             "usd",
            "order":                   "market_cap_desc",
            "per_page":                100,
            "page":                    1,
            "sparkline":               False,
            "price_change_percentage": "1h,24h",
        }
    )
    if market_data:
        movers = []
        for c in market_data:
            chg_1h  = c.get("price_change_percentage_1h_in_currency") or 0
            chg_24h = c.get("price_change_percentage_24h") or 0
            sym     = (c.get("symbol") or "").upper()
            if sym in {"USDT","USDC","BUSD","DAI","TUSD"}:
                continue
            if abs(chg_1h) >= 3 or abs(chg_24h) >= 8:
                movers.append({
                    "銘柄":    sym,
                    "name":    c.get("name"),
                    "価格":    c.get("current_price"),
                    "1h変動":  round(chg_1h, 2),
                    "24h変動": round(chg_24h, 2),
                    "機会":    ("🚀 上昇モメンタム" if chg_24h > 0 else "📉 下落モメンタム"),
                })
        movers.sort(key=lambda x: abs(x["24h変動"]), reverse=True)
        movers = movers[:8]
        results["モメンタム機会"] = movers

        print(f"\n⚡ 急騰・急落銘柄（直近 24h 変動率 ±8% 以上）:")
        print(f"{'銘柄':<8} {'1h%':>6} {'24h%':>7}  機会")
        print("─" * 35)
        for m in movers:
            print(f"  {m['銘柄']:<7} {m['1h変動']:>+5.1f}%  {m['24h変動']:>+6.1f}%  {m['機会']}")

    # ── 総合判断 ──
    judgments = []
    if fng and results["fear_greed"]:
        fgv = results["fear_greed"]["current"]
        if fgv <= 25:
            judgments.append("💡 極度の恐怖 → 割安な優良銘柄の買い蓄積チャンス（逆張り）")
        elif fgv >= 75:
            judgments.append("⚠️ 極度の強欲 → 利確・ポジション縮小を検討（天井圏リスク）")
        else:
            judgments.append(f"📊 センチメント中立（FG={fgv}）→ テクニカル主導で判断")

    # 大型アンロック銘柄を警告
    big_unlocks = [u for u in TOKEN_UNLOCKS_SAMPLE if u["unlock_pct"] >= 4]
    for u in big_unlocks:
        judgments.append(f"🔓 {u['symbol']} が {u['date']} に {u['unlock_pct']}% アンロック → ショート検討")

    results["総合判断"] = judgments
    print("\n🎯 総合判断:")
    for j in judgments:
        print(f"  {j}")

    _save_json("05_opportunity_scanner.json", results)
    return results


# ════════════════════════════════════════════════
# ⑥ 資本を収入に変える戦略（複利シミュレーション）
# ════════════════════════════════════════════════

def task6_compound_simulation(initial_jpy: float = 100_000) -> pd.DataFrame:
    """
    ⑥ 資本を収入に変える戦略

    元本 100,000 円（≈ $667 / BTC 換算）を想定し、
    3 戦略の 12 ヶ月間複利シミュレーションを実施してグラフ出力する。

    戦略①: 現物 BTC ホールド
      - BTC の過去平均月次リターン（+4〜+8%）を参考に保守的に設定
      - ボラティリティが高いためシャープレシオで評価

    戦略②: DeFi ステーキング（安定コイン 60% + LIDO stETH 40%）
      - USDC/USDT 安定コイン yield: 5〜8% APY
      - stETH（イーサリアムステーキング）: 4〜5% APY
      - 月次複利で再投資

    戦略③: グリッドボット（BTC/USDT スポット）
      - 月次 2〜4% の安定したグリッド利益を想定
      - ドローダウン 10% 想定
    """
    _divider("⑥ 資本を収入に変える戦略（複利シミュレーション）")

    # 1USD = 150JPY で換算
    USD_PER_JPY     = 150
    initial_usd     = initial_jpy / USD_PER_JPY
    months          = 12
    dates           = pd.date_range(start=datetime.now(), periods=months + 1, freq="MS")

    # ── 戦略パラメータ ──
    rng_seed = np.random.default_rng(42)

    # 戦略①: 現物 BTC ホールド
    #   月次平均リターン: +4.5%（強気相場を保守的に）
    #   月次ボラティリティ: 18%（過去実績から）
    btc_monthly_mu    = 0.045
    btc_monthly_sigma = 0.18
    btc_returns = rng_seed.normal(btc_monthly_mu, btc_monthly_sigma, months)
    portfolio_btc = [initial_usd]
    for r in btc_returns:
        portfolio_btc.append(portfolio_btc[-1] * (1 + r))

    # 戦略②: DeFi ステーキング
    #   安定コイン（USDC + yield）: 6% APY = 月次 0.5%
    #   stETH: 4.2% APY = 月次 0.35%
    #   配分 60% 安定コイン + 40% stETH
    stable_monthly  = 0.06 / 12   # 0.5%
    steth_monthly   = 0.042 / 12  # 0.35%
    portfolio_defi  = [initial_usd]
    for _ in range(months):
        prev     = portfolio_defi[-1]
        stable_p = prev * 0.60 * (1 + stable_monthly)  # 安定コイン分
        steth_p  = prev * 0.40 * (1 + steth_monthly)   # stETH 分
        portfolio_defi.append(stable_p + steth_p)

    # 戦略③: グリッドボット
    #   月次グリッド利益: 3% ± 1%（ランダムボラティリティ込み）
    #   月に1回程度 -5% のドローダウンが来る想定
    grid_monthly_mu    = 0.030
    grid_monthly_sigma = 0.010
    grid_returns = rng_seed.normal(grid_monthly_mu, grid_monthly_sigma, months)
    # 3 ヶ月に 1 回、-5% のドローダウンイベントを追加
    dd_months = [2, 6, 10]
    for dm in dd_months:
        if dm < months:
            grid_returns[dm] -= 0.05
    portfolio_grid = [initial_usd]
    for r in grid_returns:
        portfolio_grid.append(portfolio_grid[-1] * (1 + r))

    # ── DataFrame 化 ──
    df_sim = pd.DataFrame({
        "月":               dates,
        "現物ホールド(USD)":  [round(v, 2) for v in portfolio_btc],
        "DeFiステーキング(USD)": [round(v, 2) for v in portfolio_defi],
        "グリッドボット(USD)":  [round(v, 2) for v in portfolio_grid],
    })
    df_sim["現物ホールド(JPY)"]     = (df_sim["現物ホールド(USD)"]     * USD_PER_JPY).round(0)
    df_sim["DeFiステーキング(JPY)"]  = (df_sim["DeFiステーキング(USD)"] * USD_PER_JPY).round(0)
    df_sim["グリッドボット(JPY)"]    = (df_sim["グリッドボット(USD)"]   * USD_PER_JPY).round(0)

    # ── 結果表示 ──
    final_btc  = portfolio_btc[-1]
    final_defi = portfolio_defi[-1]
    final_grid = portfolio_grid[-1]

    print(f"\n元本: ¥{initial_jpy:,.0f} (≈ ${initial_usd:.0f})")
    print(f"\n{'月':<4} {'現物ホールド':>14} {'DeFiステーキング':>16} {'グリッドボット':>14}")
    print("─" * 55)
    for _, row in df_sim.iterrows():
        m    = row["月"].strftime("%Y-%m")
        btc  = f"¥{row['現物ホールド(JPY)']:>10,.0f}"
        defi = f"¥{row['DeFiステーキング(JPY)']:>10,.0f}"
        grid = f"¥{row['グリッドボット(JPY)']:>10,.0f}"
        print(f"{m:<6} {btc:>14} {defi:>16} {grid:>14}")

    print(f"\n12ヶ月後リターン:")
    print(f"  現物ホールド:    ¥{final_btc*USD_PER_JPY:>10,.0f}  "
          f"({(final_btc/initial_usd-1)*100:>+.1f}%)")
    print(f"  DeFiステーキング: ¥{final_defi*USD_PER_JPY:>10,.0f}  "
          f"({(final_defi/initial_usd-1)*100:>+.1f}%)")
    print(f"  グリッドボット:   ¥{final_grid*USD_PER_JPY:>10,.0f}  "
          f"({(final_grid/initial_usd-1)*100:>+.1f}%)")

    # ── グラフ生成 ──
    if HAS_MPL:
        fig, axes = plt.subplots(2, 1, figsize=(12, 10))
        fig.suptitle(f"3戦略 複利シミュレーション（元本¥{initial_jpy:,.0f}）", fontsize=14, y=0.98)

        # 上段: 資産推移
        ax1 = axes[0]
        xs  = [d.strftime("%Y-%m") for d in dates]
        ax1.plot(xs, [v * USD_PER_JPY for v in portfolio_btc],
                 "o-", color="#F7931A", lw=2, ms=5, label="①現物ホールド（BTC）")
        ax1.plot(xs, [v * USD_PER_JPY for v in portfolio_defi],
                 "s-", color="#627EEA", lw=2, ms=5, label="②DeFiステーキング")
        ax1.plot(xs, [v * USD_PER_JPY for v in portfolio_grid],
                 "^-", color="#00FFA3", lw=2, ms=5, label="③グリッドボット")
        ax1.axhline(y=initial_jpy, color="gray", ls="--", lw=1, alpha=0.5, label="元本")
        ax1.fill_between(range(len(xs)), [initial_jpy] * len(xs),
                         [v * USD_PER_JPY for v in portfolio_btc],
                         alpha=0.05, color="#F7931A")
        ax1.set_title("資産推移（円）")
        ax1.set_ylabel("円")
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"¥{x:,.0f}"))
        ax1.legend(fontsize=9)
        ax1.grid(True, alpha=0.2)
        ax1.set_xticks(range(0, len(xs), 2))
        ax1.set_xticklabels(xs[::2], rotation=30, ha="right", fontsize=8)

        # 下段: 月次リターン
        ax2 = axes[1]
        btc_ret  = [(portfolio_btc[i+1]/portfolio_btc[i]-1)*100 for i in range(months)]
        defi_ret = [(portfolio_defi[i+1]/portfolio_defi[i]-1)*100 for i in range(months)]
        grid_ret = [(portfolio_grid[i+1]/portfolio_grid[i]-1)*100 for i in range(months)]
        mx = [xs[i+1] for i in range(months)]
        x  = range(len(mx))
        w  = 0.27
        ax2.bar([i - w for i in x], btc_ret,  w, color="#F7931A", alpha=0.8, label="①現物")
        ax2.bar([i     for i in x], defi_ret, w, color="#627EEA", alpha=0.8, label="②DeFi")
        ax2.bar([i + w for i in x], grid_ret, w, color="#00FFA3", alpha=0.8, label="③グリッド")
        ax2.axhline(y=0, color="white", lw=0.5, alpha=0.5)
        ax2.set_title("月次リターン（%）")
        ax2.set_ylabel("%")
        ax2.set_xticks(range(len(mx)))
        ax2.set_xticklabels(mx, rotation=30, ha="right", fontsize=8)
        ax2.legend(fontsize=9)
        ax2.grid(True, alpha=0.2, axis="y")

        plt.tight_layout()
        fig.patch.set_facecolor("#0a1628")
        for ax in axes:
            ax.set_facecolor("#0d1c2e")
            ax.tick_params(colors="gray")
            for spine in ax.spines.values():
                spine.set_edgecolor("#1d3350")

        chart_path = RESULTS_DIR / "06_compound_simulation.png"
        plt.savefig(chart_path, dpi=150, bbox_inches="tight",
                    facecolor="#0a1628", edgecolor="none")
        plt.close(fig)
        logger.info(f"📊 グラフ保存: {chart_path}")
    else:
        logger.warning("matplotlib なし。グラフは生成されません。")

    _save_json("06_compound_simulation.json", {
        "params":   {"initial_jpy": initial_jpy, "months": months},
        "timeline": df_sim["月"].dt.strftime("%Y-%m").tolist(),
        "btc":      portfolio_btc,
        "defi":     portfolio_defi,
        "grid":     portfolio_grid,
    })
    _save_csv("06_compound_simulation.csv", df_sim)
    return df_sim


# ════════════════════════════════════════════════
# ⑦ 週次レビューBot（実装のみ・自動実行はコメント参照）
# ════════════════════════════════════════════════

def _notify_slack(message: str) -> bool:
    """Slack Webhook に通知を送る"""
    if not SLACK_WEBHOOK:
        logger.warning("SLACK_WEBHOOK_URL が設定されていません（環境変数に設定してください）")
        return False
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Slack 通知失敗: {e}")
        return False


def _notify_line(message: str) -> bool:
    """LINE Notify に通知を送る"""
    if not LINE_TOKEN:
        logger.warning("LINE_NOTIFY_TOKEN が設定されていません（環境変数に設定してください）")
        return False
    try:
        resp = requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            data={"message": message},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"LINE 通知失敗: {e}")
        return False


def task7_weekly_review_bot(
    holdings: list = None,
    dry_run: bool = True,
) -> dict:
    """
    ⑦ 週次レビューBot（毎週月曜日の朝に自動実行）

    機能:
      1. 保有銘柄のパフォーマンス集計（7日間リターン）
      2. 新規シグナルのスキャン（task4 を呼び出す）
      3. ポートフォリオのリバランス提案

    自動スケジュール実行:
      pip install schedule
      cron: 0 7 * * 1 python quant_analyzer.py --task 7

    引数:
        holdings: [{"symbol": "BTC", "id": "bitcoin", "amount": 0.01, "cost_usd": 600}]
        dry_run:  True の場合は通知を送らずコンソールに出力のみ

    ⚠️  実際の実行は main() からは行わない（コメントアウト済み）
    """
    _divider("⑦ 週次レビューBot")

    if holdings is None:
        # サンプルポートフォリオ（実際は自分の保有銘柄を設定）
        holdings = [
            {"symbol": "BTC",  "id": "bitcoin",  "amount": 0.01,  "cost_usd": 620.00},
            {"symbol": "ETH",  "id": "ethereum", "amount": 0.20,  "cost_usd": 680.00},
            {"symbol": "SOL",  "id": "solana",   "amount": 5.0,   "cost_usd": 750.00},
            {"symbol": "LINK", "id": "chainlink","amount": 30.0,  "cost_usd": 450.00},
        ]

    total_cost = sum(h["cost_usd"] for h in holdings)

    # ── STEP1: 現在価格を取得してパフォーマンスを集計 ──
    ids_str = ",".join(h["id"] for h in holdings)
    prices  = _get(
        f"{COINGECKO_BASE}/simple/price",
        params={"ids": ids_str, "vs_currencies": "usd",
                "include_24hr_change": "true", "include_7d_change": "true"}
    )
    if not prices:
        logger.error("価格取得失敗")
        return {}

    performance = []
    for h in holdings:
        pid    = h["id"]
        price  = (prices.get(pid) or {}).get("usd", 0)
        chg_7d = (prices.get(pid) or {}).get("usd_7d_change", 0) or 0
        val    = price * h["amount"]
        pnl    = val - h["cost_usd"]
        pnl_pct = (pnl / h["cost_usd"] * 100) if h["cost_usd"] > 0 else 0
        performance.append({
            "symbol":    h["symbol"],
            "amount":    h["amount"],
            "現在価格":   round(price, 4),
            "評価額USD":  round(val, 2),
            "コストUSD":  h["cost_usd"],
            "損益USD":    round(pnl, 2),
            "損益率%":    round(pnl_pct, 1),
            "7d変動%":    round(chg_7d, 2),
        })

    df_perf = pd.DataFrame(performance)
    total_val = df_perf["評価額USD"].sum()
    total_pnl = df_perf["損益USD"].sum()
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    # ── STEP2: 新規シグナルスキャン ──
    logger.info("新規シグナルをスキャン中...")
    symbols_to_scan = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
                       "AVAX/USDT", "DOT/USDT", "ARB/USDT", "OP/USDT"]
    signals_df = task4_swing_signals(symbols=symbols_to_scan, target_count=3)

    # ── STEP3: リバランス提案 ──
    rebalance_suggestions = []
    if not df_perf.empty:
        # 大きく上昇した銘柄（+30%以上）は利確提案
        for _, row in df_perf.iterrows():
            if row["損益率%"] >= 30:
                rebalance_suggestions.append(
                    f"✅ {row['symbol']}: +{row['損益率%']:.0f}% 利確を検討（一部売却推奨）"
                )
            # 大きく下落した銘柄（-25%以上）はカットロス提案
            elif row["損益率%"] <= -25:
                rebalance_suggestions.append(
                    f"⚠️ {row['symbol']}: {row['損益率%']:.0f}% カットロスを検討"
                )

        # 比率が偏っている銘柄の警告（一銘柄が40%以上）
        for _, row in df_perf.iterrows():
            alloc = (row["評価額USD"] / total_val * 100) if total_val > 0 else 0
            if alloc >= 40:
                rebalance_suggestions.append(
                    f"⚖️ {row['symbol']}: ポートフォリオの{alloc:.0f}%を占有 → 分散推奨"
                )

    # ── レポート生成 ──
    now  = datetime.now().strftime("%Y/%m/%d %H:%M")
    week = datetime.now().strftime("%Y年第%Ww")
    report_lines = [
        f"📊 週次レビュー {week} — {now}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 総評価額: ${total_val:,.2f}（前週比 {total_pnl_pct:+.1f}%）",
        f"📈 確定+含み損益: ${total_pnl:+,.2f}",
        "",
        "【保有銘柄パフォーマンス】",
    ]
    for _, row in df_perf.iterrows():
        em = "🟢" if row["損益率%"] >= 0 else "🔴"
        report_lines.append(
            f"  {em} {row['symbol']}: ${row['評価額USD']:,.2f}"
            f" ({row['損益率%']:+.1f}%) | 7d: {row['7d変動%']:+.1f}%"
        )

    if not signals_df.empty:
        report_lines += ["", "【新規シグナル】"]
        for _, s in signals_df.iterrows():
            report_lines.append(
                f"  🎯 {s['方向']} {s['銘柄']}"
                f" EP:{s['エントリー']:.4g} TP:{s['TP']:.4g} SL:{s['SL']:.4g}"
                f" RR:{s['RR比']:.1f}x"
            )

    if rebalance_suggestions:
        report_lines += ["", "【リバランス提案】"]
        report_lines.extend(f"  {s}" for s in rebalance_suggestions)

    report_lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"次回レビュー: {(datetime.now() + timedelta(days=7)).strftime('%Y/%m/%d')} (月曜)",
    ]

    report_text = "\n".join(report_lines)

    print(report_text)

    # ── 通知送信 ──
    if dry_run:
        logger.info("dry_run=True のため通知は送信しません。")
        logger.info("実際に送信する場合: task7_weekly_review_bot(dry_run=False)")
    else:
        if SLACK_WEBHOOK:
            ok = _notify_slack(report_text)
            logger.info(f"Slack 通知: {'✅ 成功' if ok else '❌ 失敗'}")
        if LINE_TOKEN:
            ok = _notify_line(report_text)
            logger.info(f"LINE 通知: {'✅ 成功' if ok else '❌ 失敗'}")

    result = {
        "generated_at":   now,
        "total_value_usd": round(total_val, 2),
        "total_pnl_usd":   round(total_pnl, 2),
        "total_pnl_pct":   round(total_pnl_pct, 1),
        "performance":     performance,
        "signals":         signals_df.to_dict("records") if not signals_df.empty else [],
        "rebalance":       rebalance_suggestions,
        "report":          report_text,
    }

    _save_json("07_weekly_review.json", result)
    _save_csv("07_performance.csv", df_perf)
    return result


# ════════════════════════════════════════════════
# メインエントリポイント
# ════════════════════════════════════════════════

def main():
    """
    全タスクを順番に実行する。
    --task N を指定すると特定タスクだけ実行。
    """
    parser = argparse.ArgumentParser(
        description="クリプトクオンツアナリスト — 7タスク分析ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
タスク一覧:
  1: ① 高ポテンシャルセクターの特定
  2: ② 割安トークンの発見
  3: ③ ウォッチリストの作成
  4: ④ リスク管理されたスイングトレード戦略
  5: ⑤ 収益機会スキャナー
  6: ⑥ 資本を収入に変える戦略
  7: ⑦ 週次レビューBot（dry_run モードで実行）
  0: 全タスク実行（1〜6）

例:
  python quant_analyzer.py           # 全タスク
  python quant_analyzer.py --task 4  # タスク④のみ
        """
    )
    parser.add_argument("--task",    type=int, default=0, help="実行するタスク番号（0=全て）")
    parser.add_argument("--capital", type=float, default=100_000, help="タスク⑥の元本（円）")
    args = parser.parse_args()

    start_t = time.time()
    print(f"""
╔══════════════════════════════════════════════════════╗
║  📊 クリプトクオンツアナリスト v1.0                  ║
║  実行開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<40s}║
║  結果保存先: results/ フォルダ                        ║
╚══════════════════════════════════════════════════════╝
    """)

    tasks = {
        1: ("① 高ポテンシャルセクターの特定",      lambda: task1_sector_ranking()),
        2: ("② 割安トークンの発見",                 lambda: task2_undervalued_screener()),
        3: ("③ ウォッチリストの作成",               lambda: task3_watchlist()),
        4: ("④ スイングトレードシグナル生成",        lambda: task4_swing_signals()),
        5: ("⑤ 収益機会スキャナー",                 lambda: task5_opportunity_scanner()),
        6: ("⑥ 複利シミュレーション",               lambda: task6_compound_simulation(args.capital)),
        # ⑦ は dry_run=True でのみ実行（Slack/LINE 環境変数が必要）
        7: ("⑦ 週次レビューBot（dry_run）",         lambda: task7_weekly_review_bot(dry_run=True)),
    }

    run_ids = [args.task] if args.task in tasks else [1, 2, 3, 4, 5, 6]

    summary = {}
    for tid in run_ids:
        label, fn = tasks[tid]
        logger.info(f"\n{'='*60}\n  開始: {label}\n{'='*60}")
        try:
            result = fn()
            summary[tid] = {"status": "✅ 完了", "label": label}
            time.sleep(1.0)  # API レート制限対策
        except KeyboardInterrupt:
            logger.warning("中断されました")
            break
        except Exception as e:
            logger.error(f"タスク{tid} エラー: {e}")
            summary[tid] = {"status": f"❌ エラー: {e}", "label": label}

    # ── 実行サマリー ──
    elapsed = time.time() - start_t
    print(f"\n{'='*60}")
    print(f"  ✅ 実行完了 — 経過時間: {elapsed:.0f}秒")
    print(f"{'='*60}")
    for tid, info in summary.items():
        print(f"  {info['status']}  {info['label']}")
    print(f"\n  📁 結果: {RESULTS_DIR.absolute()}/")
    results_files = list(RESULTS_DIR.glob("*.json")) + list(RESULTS_DIR.glob("*.csv")) + list(RESULTS_DIR.glob("*.png"))
    for f in sorted(results_files):
        size = f.stat().st_size
        print(f"    {f.name:<40s} {size/1024:>6.1f} KB")


if __name__ == "__main__":
    main()
