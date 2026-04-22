"""
毎年プラス・清算ゼロを目指す反復検証
=====================================
設計方針:
  1. レバレッジ 1倍のみ（清算リスク完全ゼロ）
  2. LONG/SHORT 両方向（市況に応じて切替）
  3. 厳格なBTCレジームフィルタ
  4. ストップロス（-15%）・トレーリングストップ（+20%利益時）
  5. 複数パラメータで反復テスト、年別プラス達成を探す
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125


def btc_regime_strict(btc_row, btc_history, strict=True) -> str:
    """
    厳格なレジーム判定
    BULL: BTC > EMA200 AND EMA50 > EMA200 AND BTC が直近30日の高値を更新
    BEAR: BTC < EMA200 AND EMA50 < EMA200 AND BTC が直近30日の安値を更新
    """
    if pd.isna(btc_row.get("ema200")) or pd.isna(btc_row.get("ema50")):
        return "neutral"
    p, e50, e200 = btc_row["close"], btc_row["ema50"], btc_row["ema200"]
    if strict and len(btc_history) >= 30:
        recent = btc_history.tail(30)
        if p > e200 and e50 > e200 and p >= recent["high"].max() * 0.95:
            return "bull"
        if p < e200 and e50 < e200 and p <= recent["low"].min() * 1.05:
            return "bear"
    else:
        if p > e200 and e50 > e200: return "bull"
        if p < e200 and e50 < e200: return "bear"
    return "neutral"


def run_backtest(all_data, start, end, config, initial=10_000.0):
    """
    config:
      adx_min:  エントリー閾値
      rsi_short_min: SHORT時のRSI下限
      max_pos:  最大保有数
      stop_loss_pct: ストップロス (-15%なら0.15)
      trail_activate_pct: トレーリング発動 (+20%なら0.20)
      trail_giveback_pct: トレーリング戻し幅 (0.05=5%戻したら利確)
      enable_short: SHORT許可
      strict_regime: 厳格レジーム判定
    """
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}  # sym -> {side, qty, entry_price, leverage, entry_ts, alloc_usd, peak_price}
    equity_curve = []
    trades = []

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]

        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r
        regime = btc_regime_strict(btc_row, btc_hist, strict=config["strict_regime"])

        # ━ 決済チェック ━
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            p = positions[sym]
            cur = r["close"]

            # ピーク更新（トレーリング用）
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                adverse = (p["entry_price"] - cur) / p["entry_price"]
                favorable = (p["peak_price"] - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                adverse = (cur - p["entry_price"]) / p["entry_price"]
                favorable = (p["entry_price"] - p["peak_price"]) / p["entry_price"]

            close_reason = None
            # ストップロス
            if adverse >= config["stop_loss_pct"]:
                close_reason = "stop_loss"
            # トレーリング発動＆戻し
            elif favorable >= config["trail_activate_pct"]:
                if p["side"] == "long":
                    giveback = (p["peak_price"] - cur) / p["peak_price"]
                else:
                    giveback = (cur - p["peak_price"]) / p["peak_price"]
                if giveback >= config["trail_giveback_pct"]:
                    close_reason = "trail_exit"
            # レジーム不一致
            required_regime = "bull" if p["side"] == "long" else "bear"
            if regime != required_regime:
                close_reason = close_reason or "regime"
            # 個別シグナル消失
            if r["adx"] < config["adx_min"]:
                close_reason = close_reason or "adx_low"

            if close_reason:
                if p["side"] == "long":
                    exit_p = cur * (1 - SLIP)
                    pnl = p["qty"] * (exit_p - p["entry_price"])
                else:
                    exit_p = cur * (1 + SLIP)
                    pnl = p["qty"] * (p["entry_price"] - exit_p)
                notional = p["qty"] * exit_p
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({
                    "sym": sym, "side": p["side"], "pnl": pnl,
                    "reason": close_reason, "ret_pct": pnl/p["alloc_usd"]*100,
                })
                del positions[sym]

        # ━ 新規エントリー ━
        if regime in ("bull", "bear"):
            if regime == "bear" and not config["enable_short"]:
                pass
            else:
                direction = "long" if regime == "bull" else "short"
                slots = config["max_pos"] - len(positions)
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in positions: continue
                        if r["adx"] < config["adx_min"]: continue
                        price = r["close"]
                        ema200 = r["ema200"]
                        rsi = r.get("rsi", 50)
                        if pd.isna(rsi): continue

                        if direction == "long":
                            if price > ema200 and rsi >= config.get("rsi_long_min", 0):
                                candidates.append((sym, r))
                        else:  # short
                            if price < ema200 and rsi >= config["rsi_short_min"]:
                                candidates.append((sym, r))

                    candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
                    candidates = candidates[:slots]
                    if candidates:
                        per = cash / max(slots, 1) * 0.95
                        for sym, r in candidates:
                            if per < 10: break
                            raw = r["close"]
                            if direction == "long":
                                ep = raw * (1 + SLIP)
                            else:
                                ep = raw * (1 - SLIP)
                            qty = per / ep
                            notional = per  # レバ1倍
                            cash -= per + notional * FEE
                            positions[sym] = {
                                "side": direction,
                                "qty": qty, "entry_price": ep,
                                "leverage": 1.0, "entry_ts": date,
                                "alloc_usd": per, "peak_price": ep,
                            }

        # MTM
        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"])
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur)
        equity_curve.append({"ts": date, "equity": cash + unreal})

    # 最終クローズ
    for sym in list(positions.keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= dates[-1]]
        if not ld: continue
        last_row = df.loc[ld[-1]]
        p = positions[sym]
        raw = last_row["close"]
        if p["side"] == "long":
            exit_p = raw * (1 - SLIP)
            pnl = p["qty"] * (exit_p - p["entry_price"])
        else:
            exit_p = raw * (1 + SLIP)
            pnl = p["qty"] * (p["entry_price"] - exit_p)
        notional = p["qty"] * exit_p
        pnl -= notional * FEE
        cash += p["alloc_usd"] + pnl

    # 年次集計
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        start_eq = yr.iloc[0]
        end_eq = yr.iloc[-1]
        ret_pct = (end_eq / start_eq - 1) * 100 if start_eq > 0 else 0
        yearly[y] = round(ret_pct, 2)

    peak = initial
    max_dd = 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    final = cash
    total_ret = (final - initial) / initial * 100
    return {
        "final": final, "total_ret": total_ret,
        "max_dd": max_dd,
        "yearly": yearly,
        "n_trades": len(trades),
        "n_long": sum(1 for t in trades if t["side"] == "long"),
        "n_short": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": sum(1 for t in trades if t.get("pnl", 0) > 0) / max(len(trades), 1) * 100,
        "all_years_positive": all(v > 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 50銘柄×5年データ + RSI計算中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)\n")

    # 反復: 複数パラメータを試す
    configs = [
        ("C01 基本(ADX25/SL15/TRL20) 厳格レジーム",
         {"adx_min": 25, "stop_loss_pct": 0.15, "trail_activate_pct": 0.20,
          "trail_giveback_pct": 0.05, "max_pos": 20, "rsi_short_min": 70,
          "enable_short": True, "strict_regime": True}),
        ("C02 厳格 ADX30/SL10/TRL25",
         {"adx_min": 30, "stop_loss_pct": 0.10, "trail_activate_pct": 0.25,
          "trail_giveback_pct": 0.05, "max_pos": 20, "rsi_short_min": 75,
          "enable_short": True, "strict_regime": True}),
        ("C03 超厳格 ADX35/SL10/RSI80",
         {"adx_min": 35, "stop_loss_pct": 0.10, "trail_activate_pct": 0.20,
          "trail_giveback_pct": 0.05, "max_pos": 15, "rsi_short_min": 80,
          "enable_short": True, "strict_regime": True}),
        ("C04 緩レジーム + ADX30",
         {"adx_min": 30, "stop_loss_pct": 0.12, "trail_activate_pct": 0.20,
          "trail_giveback_pct": 0.05, "max_pos": 20, "rsi_short_min": 75,
          "enable_short": True, "strict_regime": False}),
        ("C05 LONG only 比較 ADX30",
         {"adx_min": 30, "stop_loss_pct": 0.10, "trail_activate_pct": 0.20,
          "trail_giveback_pct": 0.05, "max_pos": 20, "rsi_short_min": 99,
          "enable_short": False, "strict_regime": False}),
        ("C06 最安全 ADX40/SL8/RSI85",
         {"adx_min": 40, "stop_loss_pct": 0.08, "trail_activate_pct": 0.15,
          "trail_giveback_pct": 0.03, "max_pos": 10, "rsi_short_min": 85,
          "enable_short": True, "strict_regime": True}),
        ("C07 多保有50 ADX25/SL12",
         {"adx_min": 25, "stop_loss_pct": 0.12, "trail_activate_pct": 0.20,
          "trail_giveback_pct": 0.05, "max_pos": 50, "rsi_short_min": 75,
          "enable_short": True, "strict_regime": True}),
        ("C08 少数精鋭5件 ADX35/SL10",
         {"adx_min": 35, "stop_loss_pct": 0.10, "trail_activate_pct": 0.25,
          "trail_giveback_pct": 0.05, "max_pos": 5, "rsi_short_min": 80,
          "enable_short": True, "strict_regime": True}),
    ]

    print(f"{'=' * 130}")
    print(f"🎯 毎年プラス・清算ゼロ 反復探索（レバ1倍固定・LONG/SHORT）")
    print(f"{'=' * 130}")
    print(f"{'戦略':40s} | {'2020':>8s} {'2021':>8s} {'2022':>8s} {'2023':>8s} {'2024':>8s} | "
          f"{'5年計':>8s} | {'DD':>5s} | {'L/S':>8s} | {'毎年+':>5s}")
    print("-" * 130)

    best_all_positive = None
    results = {}
    for name, cfg in configs:
        r = run_backtest(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:40s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            c = "✅" if v > 0 else "❌"
            row += f"{v:>+6.1f}% "
        row += f"| {r['total_ret']:>+6.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['n_long']:>3d}/{r['n_short']:>3d} | "
        row += "🎉YES" if r["all_years_positive"] else f"{r['negative_years']}年負"
        print(row)
        results[name] = r
        if r["all_years_positive"]:
            if best_all_positive is None or r["total_ret"] > best_all_positive[1]["total_ret"]:
                best_all_positive = (name, r)

    print(f"\n{'=' * 130}")
    if best_all_positive:
        print(f"🎉 毎年プラス達成！ 最高成績: {best_all_positive[0]}")
        print(f"   5年合計: {best_all_positive[1]['total_ret']:+.1f}%")
        print(f"   DD: {best_all_positive[1]['max_dd']:.1f}%")
    else:
        print(f"⚠️ 全戦略が年次マイナスを含む。パラメータ調整が必要")
    print(f"{'=' * 130}")

    out = (Path(__file__).resolve().parent / "results" / "zero_loss_year.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
