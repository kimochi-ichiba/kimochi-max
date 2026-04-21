"""
iter48: 9改善案のうち daily バックテストで検証可能な5つを累積比較
================================================================
ベース: _iter47_trade_limit.py の H11 ハイブリッド戦略
(BTC40%マイルド + ACH40%モメンタム + USDT20%金利)

比較する8パターン (各段階で improvement を追加):
  A: iter47 ベースライン (50銘柄, Top3, 月次, 改善なし)
  B: + 銘柄拡張 (70銘柄)
  C: + Top5 + 週次リバランス (#6)
  D: + RSI<70 フィルター (#2)
  E: + トレーリングストップ -5% (#1)
  F: + 部分利確 +5%/+10%/+20% (#4)
  G: + 急落時休業 (24h-10%で48h停止, #9)
  H: 全部入り = G と同じ (累積確認)

daily 足では検証できない #3(指値)、#5(MTF)、#7(マルチアセット)、
#8(アンサンブル) は HTML に注記し、Phase 3-4 で別途実装。

データ: Binance 日足 2020-01-01〜2024-12-31 実データのみ
(合成データ・架空データ一切不使用)
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np

from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source
from _rsi_short_backtest import fetch_with_rsi
import _iter43_rethink as R43

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter48_all_improvements.json"
OUT_HTML = RESULTS_DIR / "iter48_report.html"

FEE = 0.0006  # 市場注文想定 (iter47と揃える)
SLIP = 0.0003

# 追加銘柄 (Phase0 FAIL 銘柄の代替 + 主要アルト20銘柄)
UNIVERSE_ADD = [
    "POL/USDT",    # MATIC のリネーム先
    "TON/USDT",    # Toncoin
    "PEPE/USDT",   # Memecoin top
    "SHIB/USDT",   # Shiba Inu
    "ONDO/USDT",   # RWA top
    "JUP/USDT",    # Jupiter
    "WLD/USDT",    # Worldcoin
    "LDO/USDT",    # Lido
    "IMX/USDT",    # Immutable X
    "WIF/USDT",    # dogwifhat
    "ENA/USDT",    # Ethena
    "SUSHI/USDT",  # SushiSwap
    "GALA/USDT",   # Gala Games
    "QNT/USDT",    # Quant
    "JASMY/USDT",  # JasmyCoin
    "PENDLE/USDT", # Pendle
    "KAVA/USDT",   # Kava
    "MINA/USDT",   # Mina Protocol
    "RENDER/USDT", # Render
    "STRK/USDT",   # Starknet
]

# Phase0でFAILした銘柄を除外リスト (既存キャッシュから除く)
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# データロード (本番データのみ)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_base_data() -> dict:
    """既存キャッシュ 50銘柄をロード"""
    with open(CACHE_PATH, "rb") as f:
        d = pickle.load(f)
    print(f"📦 既存キャッシュ読込: {len(d)}銘柄")
    return d


def fetch_additional_symbols(base_data: dict, symbols_to_add: list) -> dict:
    """追加銘柄を Binance から取得。既存データに merge して返す"""
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    # 取得済みは除外
    need_fetch = [s for s in symbols_to_add if s not in base_data]
    if not need_fetch:
        print(f"ℹ️ 全追加銘柄が既に取得済み")
        return base_data

    print(f"🌐 Binance から {len(need_fetch)}銘柄を追加取得中...")
    try:
        added = fetch_with_rsi(fetcher, need_fetch, "2020-01-01", "2024-12-31")
    except Exception as e:
        print(f"⚠️ fetch_with_rsi エラー: {e}")
        added = {}

    # merge
    merged = dict(base_data)
    fetched_count = 0
    for sym, df in added.items():
        if df is None or len(df) < 100:
            print(f"  ⚠️ {sym}: データ不足 ({len(df) if df is not None else 0}行)")
            continue
        merged[sym] = df
        fetched_count += 1
        print(f"  ✅ {sym}: {len(df)}行 ({df.index[0].date()}〜{df.index[-1].date()})")

    print(f"📊 追加完了: {fetched_count}/{len(need_fetch)}銘柄")
    # キャッシュを更新
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(merged, f)
    print(f"💾 キャッシュ更新: {CACHE_PATH}")
    return merged


def build_universe(all_data: dict, extended: bool) -> list:
    """ベースユニバース or 拡張ユニバースを返す"""
    base = [s for s in all_data.keys() if s not in UNIVERSE_REMOVE]
    if not extended:
        # 元の50銘柄だけ(ただしFAIL除外)
        base = [s for s in base if s in _original_50_set()]
        return sorted(base)
    # 拡張: 元の50銘柄(FAIL除外) + 追加銘柄で実取得できたもの
    return sorted(base)


def _original_50_set() -> set:
    from _multipos_backtest import UNIVERSE_50
    return set(UNIVERSE_50)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 拡張版バックテストエンジン (全9改善フラグ対応)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_ach_enhanced(
    all_data: dict, universe: list, start: str, end: str,
    top_n: int = 3,
    lookback_days: int = 90,
    rebalance: str = "monthly",  # 'monthly' or 'weekly'
    rsi_max: float | None = None,  # e.g. 70 = buy only if RSI < 70
    trailing_stop_pct: float | None = None,  # e.g. 5 = sell if price drops 5% from peak
    partial_profit_levels: list | None = None,  # e.g. [5, 10, 20]
    crash_pause: bool = False,  # True = pause if BTC -10% in 24h
    initial: float = 10_000.0,
) -> dict:
    """ACH モメンタム (全改善フラグ対応版)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    # positions[sym] = {qty, entry_price, peak_price, units_sold}
    positions: dict = {}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    n_trailing_sells = 0
    n_partial_sells = 0
    n_pause_days = 0
    last_rebalance_key = None
    crash_pause_until = None

    for date in dates:
        date_str = str(date)[:10]

        # 急落時休業チェック (#9)
        if crash_pause:
            if crash_pause_until is not None and date < crash_pause_until:
                # 休業中。ただ時価評価だけ記録
                total = cash
                for sym, pos in positions.items():
                    df = all_data[sym]
                    if date in df.index:
                        total += pos["qty"] * df.loc[date, "close"]
                equity_curve.append({"ts": date, "equity": total})
                n_pause_days += 1
                continue
            # BTC 24h変化率チェック
            btc_idx = list(btc_df.index)
            try:
                today_i = btc_idx.index(date)
                if today_i > 0:
                    btc_today = btc_df.iloc[today_i]["close"]
                    btc_yesterday = btc_df.iloc[today_i - 1]["close"]
                    change_pct = (btc_today - btc_yesterday) / btc_yesterday * 100
                    if change_pct <= -10.0:
                        crash_pause_until = date + pd.Timedelta(days=2)
                        # 全決済
                        for sym, pos in list(positions.items()):
                            df = all_data[sym]
                            if date in df.index:
                                price = df.loc[date, "close"] * (1 - SLIP)
                                cash += pos["qty"] * price * (1 - FEE)
                                n_trades += 1
                                positions.pop(sym)
                        # そのまま休業
                        total = cash
                        equity_curve.append({"ts": date, "equity": total})
                        continue
            except ValueError:
                pass

        # ━ トレーリングストップ (#1) & 部分利確 (#4) ━
        # 日次で価格更新
        for sym in list(positions.keys()):
            df = all_data[sym]
            if date not in df.index:
                continue
            price = df.loc[date, "close"]
            pos = positions[sym]
            # ピーク更新
            if price > pos.get("peak_price", 0):
                pos["peak_price"] = price

            # 部分利確チェック
            if partial_profit_levels is not None:
                entry = pos["entry_price"]
                gain_pct = (price - entry) / entry * 100
                units_sold = pos.get("units_sold", 0)
                # entry を100% に対して 3段で 33/33/34 売却
                while units_sold < len(partial_profit_levels):
                    threshold = partial_profit_levels[units_sold]
                    if gain_pct >= threshold:
                        sell_fraction = 1.0 / (len(partial_profit_levels) - units_sold)
                        sell_qty = pos["qty"] * sell_fraction
                        sell_price = price * (1 - SLIP)
                        cash += sell_qty * sell_price * (1 - FEE)
                        pos["qty"] -= sell_qty
                        pos["units_sold"] += 1
                        units_sold = pos["units_sold"]
                        n_trades += 1
                        n_partial_sells += 1
                    else:
                        break

            # トレーリングストップチェック
            if trailing_stop_pct is not None and pos["qty"] > 0:
                peak = pos["peak_price"]
                drop_pct = (peak - price) / peak * 100 if peak > 0 else 0
                if drop_pct >= trailing_stop_pct:
                    # 残り全売却
                    sell_price = price * (1 - SLIP)
                    cash += pos["qty"] * sell_price * (1 - FEE)
                    positions.pop(sym)
                    n_trades += 1
                    n_trailing_sells += 1

        # ━ リバランス判定 ━
        if rebalance == "monthly":
            cur_key = (date.year, date.month)
        else:  # weekly
            iso = date.isocalendar()
            cur_key = (iso.year, iso.week)
        do_rebalance = (last_rebalance_key is None) or (cur_key != last_rebalance_key)

        if do_rebalance:
            # ━ 全決済 ━
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date not in df.index:
                    continue
                price = df.loc[date, "close"] * (1 - SLIP)
                cash += positions[sym]["qty"] * price * (1 - FEE)
                n_trades += 1
                positions.pop(sym)

            # ━ Top N 選定 ━
            scores = []
            for sym in universe:
                if sym not in all_data:
                    continue
                df = all_data[sym]
                if date not in df.index:
                    continue
                past_idx = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
                if len(past_idx) < 20:
                    continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past_idx[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20:
                    continue
                # RSI フィルター (#2)
                if rsi_max is not None:
                    rsi = df.loc[date].get("rsi", 50)
                    if pd.isna(rsi) or rsi >= rsi_max:
                        continue
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)

            # BTC レジーム (Bear だと買わない)
            btc_r = btc_df.loc[date]
            btc_price_btc = btc_r["close"]
            btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_price_btc < btc_ema200:
                last_rebalance_key = cur_key
            else:
                selected = scores[:top_n]
                if selected:
                    weight = 1.0 / len(selected)
                    for sym, _ in selected:
                        df = all_data[sym]
                        price_buy = df.loc[date, "close"] * (1 + SLIP)
                        cost = cash * weight
                        if cost > 0:
                            qty = cost / price_buy * (1 - FEE)
                            positions[sym] = {
                                "qty": qty,
                                "entry_price": df.loc[date, "close"],
                                "peak_price": df.loc[date, "close"],
                                "units_sold": 0,
                            }
                            cash -= cost
                            n_trades += 1
                last_rebalance_key = cur_key

        # ━ 時価評価 ━
        total = cash
        for sym, pos in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += pos["qty"] * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["n_trailing_sells"] = n_trailing_sells
    r["n_partial_sells"] = n_partial_sells
    r["n_pause_days"] = n_pause_days
    return r


def run_btc_mild_enhanced(
    all_data: dict, start: str, end: str,
    trailing_stop_pct: float | None = None,
    rsi_max: float | None = None,
    crash_pause: bool = False,
    initial: float = 10_000.0, cash_rate: float = 0.03,
) -> dict:
    """BTC マイルド (トレーリング・RSI・急落休業対応)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    btc_qty = 0.0
    entry_price = 0.0
    peak_price = 0.0
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    crash_pause_until = None

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]
        ema200 = r.get("ema200")
        rsi = r.get("rsi", 50)

        if crash_pause and crash_pause_until is not None and date < crash_pause_until:
            total = cash + btc_qty * price
            # 金利
            if btc_qty == 0:
                cash *= (1 + cash_rate / 365)
                total = cash
            equity_curve.append({"ts": date, "equity": total})
            continue

        if crash_pause:
            btc_idx = list(btc_df.index)
            try:
                today_i = btc_idx.index(date)
                if today_i > 0:
                    pp = btc_df.iloc[today_i - 1]["close"]
                    change_pct = (price - pp) / pp * 100
                    if change_pct <= -10.0:
                        crash_pause_until = date + pd.Timedelta(days=2)
                        if btc_qty > 0:
                            sell_p = price * (1 - SLIP)
                            cash += btc_qty * sell_p * (1 - FEE)
                            btc_qty = 0
                            n_trades += 1
                        equity_curve.append({"ts": date, "equity": cash})
                        continue
            except ValueError:
                pass

        # BUY
        can_buy = (btc_qty == 0) and (not pd.isna(ema200)) and (price > ema200)
        if rsi_max is not None:
            can_buy = can_buy and (not pd.isna(rsi)) and (rsi < rsi_max)
        if can_buy:
            buy_p = price * (1 + SLIP)
            btc_qty = cash / buy_p * (1 - FEE)
            entry_price = price
            peak_price = price
            cash = 0
            n_trades += 1

        # 保有中: peak更新
        if btc_qty > 0:
            if price > peak_price:
                peak_price = price
            # トレーリングストップ
            sold = False
            if trailing_stop_pct is not None:
                drop_pct = (peak_price - price) / peak_price * 100 if peak_price > 0 else 0
                if drop_pct >= trailing_stop_pct:
                    sell_p = price * (1 - SLIP)
                    cash += btc_qty * sell_p * (1 - FEE)
                    btc_qty = 0
                    n_trades += 1
                    sold = True
            # SELL by EMA200 (saved if not already sold by trailing)
            if not sold and (not pd.isna(ema200)) and price < ema200:
                sell_p = price * (1 - SLIP)
                cash += btc_qty * sell_p * (1 - FEE)
                btc_qty = 0
                n_trades += 1

        # Cashは年率3%金利
        if btc_qty == 0:
            cash *= (1 + cash_rate / 365)

        total = cash + btc_qty * price
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    return r


def run_h11_hybrid_enhanced(
    all_data: dict, universe: list, start: str, end: str,
    top_n: int = 3, rebalance: str = "monthly",
    rsi_max: float | None = None,
    trailing_stop_pct: float | None = None,
    partial_profit_levels: list | None = None,
    crash_pause: bool = False,
    initial: float = 10_000.0,
) -> dict:
    """H11: BTC40% + ACH40% + USDT20%"""
    btc_w, ach_w, usdt_w = 0.40, 0.40, 0.20
    btc_res = run_btc_mild_enhanced(
        all_data, start, end,
        trailing_stop_pct=trailing_stop_pct,
        rsi_max=rsi_max,
        crash_pause=crash_pause,
        initial=initial * btc_w, cash_rate=0.03,
    )
    ach_res = run_ach_enhanced(
        all_data, universe, start, end,
        top_n=top_n, lookback_days=90, rebalance=rebalance,
        rsi_max=rsi_max,
        trailing_stop_pct=trailing_stop_pct,
        partial_profit_levels=partial_profit_levels,
        crash_pause=crash_pause,
        initial=initial * ach_w,
    )
    # USDT (年率3%金利)
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    usdt_eq = initial * usdt_w
    usdt_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": usdt_eq}]
    for date in dates:
        usdt_eq *= (1 + 0.03 / 365)
        usdt_curve.append({"ts": date, "equity": usdt_eq})

    # 統合 equity
    btc_eq = pd.DataFrame(btc_res["equity_curve"]).set_index("ts") if "equity_curve" in btc_res else None
    ach_eq = pd.DataFrame(ach_res["equity_curve"]).set_index("ts") if "equity_curve" in ach_res else None
    # summarize は equity_curve を返さない。再計算が必要なら別手。
    # 簡易: final のみ合成
    combined_final = (btc_res["final"] + ach_res["final"] + usdt_eq)
    combined_ret_pct = (combined_final / initial - 1) * 100
    # 年別も単純合成は正確ではないが、パターン比較目的なら許容
    years = sorted(set(list(btc_res.get("yearly", {}).keys()) + list(ach_res.get("yearly", {}).keys())))
    # 年別リターンは加重平均で合成 (近似)
    yearly = {}
    for y in years:
        bb = btc_res.get("yearly", {}).get(y, 0)
        aa = ach_res.get("yearly", {}).get(y, 0)
        yearly[y] = round(bb * btc_w + aa * ach_w + 0.03 * usdt_w * 100, 2)

    return {
        "final": round(combined_final, 2),
        "total_ret": round(combined_ret_pct, 2),
        "avg_annual_ret": round((combined_final / initial) ** (1/5) * 100 - 100, 2),
        "yearly": yearly,
        "max_dd": max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0)),
        "sharpe": round((btc_res.get("sharpe", 0) * btc_w + ach_res.get("sharpe", 0) * ach_w), 2),
        "n_trades": btc_res.get("n_trades", 0) + ach_res.get("n_trades", 0),
        "n_trades_btc": btc_res.get("n_trades", 0),
        "n_trades_ach": ach_res.get("n_trades", 0),
        "n_trailing_sells": ach_res.get("n_trailing_sells", 0),
        "n_partial_sells": ach_res.get("n_partial_sells", 0),
        "n_pause_days": ach_res.get("n_pause_days", 0),
        "equity_weekly": ach_res.get("equity_weekly", []),  # ACH only (近似)
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# パターン定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATTERNS = [
    {"id": "A", "name": "iter47 ベースライン",
     "desc": "50銘柄・Top3・月次・改善なし (現行)", "extended": False,
     "top_n": 3, "rebalance": "monthly", "rsi_max": None,
     "trailing_stop_pct": None, "partial_profit_levels": None, "crash_pause": False},

    {"id": "B", "name": "+ 銘柄拡張",
     "desc": "70銘柄 (+POL/TON/PEPE 等)", "extended": True,
     "top_n": 3, "rebalance": "monthly", "rsi_max": None,
     "trailing_stop_pct": None, "partial_profit_levels": None, "crash_pause": False},

    {"id": "C", "name": "+ Top5 + 週次",
     "desc": "機会拡大 (#6)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": None,
     "trailing_stop_pct": None, "partial_profit_levels": None, "crash_pause": False},

    {"id": "D", "name": "+ RSI<70 フィルター",
     "desc": "買われすぎ除外 (#2)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": 70,
     "trailing_stop_pct": None, "partial_profit_levels": None, "crash_pause": False},

    {"id": "E", "name": "+ トレーリングストップ-5%",
     "desc": "最高値-5%で自動売却 (#1)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": 70,
     "trailing_stop_pct": 5.0, "partial_profit_levels": None, "crash_pause": False},

    {"id": "F", "name": "+ 部分利確",
     "desc": "+5%/+10%/+20%で3分割売り (#4)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": 70,
     "trailing_stop_pct": 5.0, "partial_profit_levels": [5, 10, 20], "crash_pause": False},

    {"id": "G", "name": "+ 急落時休業",
     "desc": "24h-10%で48h停止 (#9)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": 70,
     "trailing_stop_pct": 5.0, "partial_profit_levels": [5, 10, 20], "crash_pause": True},

    {"id": "H", "name": "【全改善 最強版】",
     "desc": "G と同じ (全部入り = 累積で同一)", "extended": True,
     "top_n": 5, "rebalance": "weekly", "rsi_max": 70,
     "trailing_stop_pct": 5.0, "partial_profit_levels": [5, 10, 20], "crash_pause": True},
]


def main():
    print("=" * 70)
    print("🚀 iter48: 9改善案 累積比較バックテスト")
    print("=" * 70)

    print("\n📦 データロード...")
    base = load_base_data()

    print("\n🌐 追加銘柄取得...")
    all_data = fetch_additional_symbols(base, UNIVERSE_ADD)
    print(f"📊 総銘柄数: {len(all_data)}")

    universe_base = build_universe(all_data, extended=False)
    universe_ext = build_universe(all_data, extended=True)
    print(f"🎯 ベースユニバース: {len(universe_base)}銘柄 (FAIL除外後)")
    print(f"🎯 拡張ユニバース: {len(universe_ext)}銘柄")

    results = []
    for i, p in enumerate(PATTERNS, 1):
        print(f"\n[{i}/{len(PATTERNS)}] パターン{p['id']}: {p['name']}")
        print(f"       {p['desc']}")
        universe = universe_ext if p["extended"] else universe_base
        t0 = time.time()
        r = run_h11_hybrid_enhanced(
            all_data, universe, "2020-01-01", "2024-12-31",
            top_n=p["top_n"], rebalance=p["rebalance"],
            rsi_max=p["rsi_max"],
            trailing_stop_pct=p["trailing_stop_pct"],
            partial_profit_levels=p["partial_profit_levels"],
            crash_pause=p["crash_pause"],
        )
        elapsed = time.time() - t0
        r.update({"id": p["id"], "name": p["name"], "desc": p["desc"],
                  "universe_size": len(universe),
                  "config": {k: v for k, v in p.items() if k not in ("id", "name", "desc")},
                  "elapsed_sec": round(elapsed, 2)})
        results.append(r)
        print(f"  ✅ {elapsed:.1f}s | 最終 ${r['final']:,.0f} | リターン {r['total_ret']:+.2f}% | "
              f"DD {r['max_dd']:.1f}% | 取引 {r['n_trades']}回")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter48_all_improvements.py",
        "data_source": "Binance daily (2020-01-01 〜 2024-12-31, 実データ)",
        "universe_base_size": len(universe_base),
        "universe_ext_size": len(universe_ext),
        "universe_added": UNIVERSE_ADD,
        "universe_removed": UNIVERSE_REMOVE,
        "initial": 10000,
        "improvements_tested": ["#1 trailing stop", "#2 RSI filter",
                                 "#4 partial take-profit", "#6 top5/weekly",
                                 "#9 crash pause"],
        "improvements_not_tested": ["#3 limit orders (execution-level)",
                                      "#5 multi-timeframe (needs 1h/4h data)",
                                      "#7 multi-asset BTC+ETH+SOL (architecture)",
                                      "#8 strategy ensemble (multiple strategies)"],
        "patterns": results,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 JSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
