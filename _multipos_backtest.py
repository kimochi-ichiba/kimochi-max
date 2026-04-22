"""
マルチポジション DL MAX 2x バックテスト
==============================================
ライブボットと同じロジックで 2020-2024 実Binanceデータを検証
 - BTCレジーム (EMA50>EMA200 & close>EMA200) でのみ取引許可
 - 各銘柄独立に DL MAX 2x 判定 (ADX>=20 なら 1x, >=30 なら 2x)
 - 空きスロットには ADX 高い順で自動エントリー
 - スロット数ごとの成績差を比較
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125

# 50銘柄ユニバース（Binance上場・2020以前から取引実績あり）
UNIVERSE_50 = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "DOGE/USDT",
    "LTC/USDT", "BCH/USDT", "ATOM/USDT", "UNI/USDT", "NEAR/USDT",
    "FIL/USDT", "TRX/USDT", "ETC/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "ALGO/USDT", "XLM/USDT", "VET/USDT", "HBAR/USDT",
    "EGLD/USDT", "FTM/USDT", "AAVE/USDT", "SAND/USDT", "MANA/USDT",
    "CRV/USDT", "COMP/USDT", "SUSHI/USDT", "YFI/USDT", "SNX/USDT",
    "MKR/USDT", "IMX/USDT", "INJ/USDT", "GRT/USDT", "ICP/USDT",
    "KAVA/USDT", "ZEC/USDT", "DASH/USDT", "ZIL/USDT", "ONE/USDT",
    "BAT/USDT", "ENJ/USDT", "QNT/USDT", "CHZ/USDT", "AXS/USDT",
]


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    up = df["high"] - df["high"].shift()
    dn = df["low"].shift() - df["low"]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).mean() / df["atr"]
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / df["atr"]
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(14).mean()
    return df


def fetch_all_data(fetcher, symbols, start, end, buf_days=320):
    """全銘柄の日足データを取得（ハルシネーション6項目検証）"""
    assert_binance_source(fetcher)
    buf_start = (pd.Timestamp(start) - pd.Timedelta(days=buf_days)).strftime("%Y-%m-%d")
    data = {}
    skipped = []
    for sym in symbols:
        try:
            df = fetcher.fetch_historical_ohlcv(sym, "1d", buf_start, end)
            if df.empty:
                skipped.append(sym)
                continue
            validate_ohlcv_data(df, sym, "1d")
            data[sym] = compute_indicators(df)
        except Exception as e:
            skipped.append(sym)
    if skipped:
        print(f"  ⚠️ 取得不可 {len(skipped)}銘柄 (例: {skipped[:5]})")
    return data


def target_leverage(row) -> float:
    price = row["close"]
    if not (price > row["ema200"] and row["ema50"] > row["ema200"]):
        return 0.0
    adx = row["adx"]
    if pd.isna(adx):
        return 0.0
    if adx >= 30: return 2.0
    if adx >= 20: return 1.0
    return 0.0


def run_backtest(all_data, start, end, max_positions, initial=10_000.0, verbose=False):
    """
    ライブボットと同じロジックで期間通期バックテスト
    """
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)

    # 全銘柄共通の日付リスト（全銘柄が存在する日のみ使用）
    syms = list(all_data.keys())
    if "BTC/USDT" not in syms:
        raise RuntimeError("BTC/USDTがない")
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}  # sym -> {qty, entry_price, leverage, entry_ts, alloc_usd}
    equity_curve = []
    trades = []

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        if pd.isna(btc_row["ema200"]):
            equity_curve.append({"ts": date, "equity": cash})
            continue
        btc_bull = (btc_row["close"] > btc_row["ema200"]
                    and btc_row["ema50"] > btc_row["ema200"])

        # 全銘柄の当日データ
        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date not in df.index:
                continue
            r = df.loc[date]
            if pd.isna(r.get("ema200")) or pd.isna(r.get("adx")):
                continue
            today_rows[sym] = r

        # 決済判定: BTC弱気 or 個別シグナル消失
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            lev = target_leverage(r) if btc_bull else 0.0
            if lev == 0:
                # 決済
                p = positions[sym]
                exit_price = r["close"] * (1 - SLIP)
                pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
                notional = p["qty"] * exit_price * p["leverage"]
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({"symbol": sym, "pnl": pnl, "hold_h": hold_h})
                del positions[sym]

        # 新規エントリー
        if btc_bull:
            open_slots = max_positions - len(positions)
            if open_slots > 0:
                candidates = sorted(
                    [(sym, r) for sym, r in today_rows.items()
                     if sym not in positions and target_leverage(r) > 0],
                    key=lambda x: x[1]["adx"], reverse=True
                )[:open_slots]
                if candidates:
                    per_slot = cash / max(open_slots, 1)
                    for sym, r in candidates:
                        if per_slot < 10:
                            break
                        lev = target_leverage(r)
                        entry_price = r["close"] * (1 + SLIP)
                        qty = per_slot / entry_price
                        notional = per_slot * lev
                        cash -= per_slot + notional * FEE
                        positions[sym] = {
                            "qty": qty, "entry_price": entry_price,
                            "leverage": lev, "entry_ts": date,
                            "alloc_usd": per_slot,
                        }

        # mark-to-market
        unreal = sum(
            p["qty"] * (today_rows[sym]["close"] - p["entry_price"]) * p["leverage"]
            for sym, p in positions.items() if sym in today_rows
        )
        equity_curve.append({"ts": date, "equity": cash + unreal})

    # 最終クローズ
    final_date = dates[-1]
    for sym in list(positions.keys()):
        df = all_data[sym]
        last_date = [d for d in df.index if d <= final_date]
        if not last_date:
            continue
        last_row = df.loc[last_date[-1]]
        p = positions[sym]
        exit_price = last_row["close"] * (1 - SLIP)
        pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
        notional = p["qty"] * exit_price * p["leverage"]
        pnl -= notional * FEE
        cash += p["alloc_usd"] + pnl
        trades.append({"symbol": sym, "pnl": pnl})

    # 統計
    final = cash
    total_ret = (final - initial) / initial * 100

    # 月次リターン計算
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    monthly = eq_df["equity"].resample("ME").last().pct_change().dropna() * 100

    # DD
    peak = initial
    max_dd = 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "final": final,
        "total_ret": total_ret,
        "monthly_avg": monthly.mean() if len(monthly) else 0,
        "monthly_median": monthly.median() if len(monthly) else 0,
        "monthly_std": monthly.std(ddof=0) if len(monthly) else 0,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "win_rate": sum(1 for t in trades if t.get("pnl", 0) > 0) / max(len(trades), 1) * 100,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 データ取得開始（本番Binance・合成禁止・6項目健全性検証）")
    print(f"   対象: {len(UNIVERSE_50)}銘柄 × 5年（2020-2024）")
    t0 = time.time()
    all_data = fetch_all_data(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄の実データ取得完了 ({time.time()-t0:.1f}秒)")

    # 検証パターン
    periods = [
        ("2020-01-01", "2024-12-31", "5年通期"),
        ("2023-01-01", "2024-12-31", "2年 (強気)"),
        ("2021-01-01", "2021-12-31", "2021 暴落"),
        ("2022-01-01", "2022-12-31", "2022 ベア"),
    ]

    max_pos_configs = [1, 5, 10, 20, 50]

    print(f"\n{'=' * 110}")
    print(f"🔬 保有件数別 成績比較（DL MAX 2x マルチポジション版）")
    print(f"{'=' * 110}")

    results = {}
    for start, end, label in periods:
        print(f"\n▶ {label} ({start} 〜 {end})")
        print(f"  {'保有数':>6s} | {'最終残高':>12s} | {'総リターン':>10s} | {'月平均':>8s} | {'最大DD':>7s} | {'取引数':>6s} | {'勝率':>6s}")
        print("  " + "-" * 100)
        for mp in max_pos_configs:
            try:
                r = run_backtest(all_data, start, end, mp)
                key = f"{label}_mp{mp}"
                results[key] = r
                print(f"  {mp:>4d}件 | ${r['final']:>10,.0f} | {r['total_ret']:+8.1f}% | "
                      f"{r['monthly_avg']:+6.2f}% | {r['max_dd']:>5.1f}% | {r['n_trades']:>5d} | "
                      f"{r['win_rate']:>5.1f}%")
            except Exception as e:
                print(f"  {mp:>4d}件 | ERROR: {e}")

    out = (Path(__file__).resolve().parent / "results" / "multipos_backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({k: v for k, v in results.items()}, indent=2,
                                ensure_ascii=False, default=str))
    print(f"\n💾 保存: {out}")
