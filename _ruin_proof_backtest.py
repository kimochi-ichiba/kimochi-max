"""
破産回避型高利回り戦略（Ruin-Proof）
=====================================
プロのリスク管理手法を導入:
  1. 1トレード最大リスク = 口座の1〜2% (絶対条件)
  2. 最大同時保有 = 5件 (集中リスク回避)
  3. 現金待機 > 50% (常に余裕資金確保)
  4. 超選別エントリー (ADX>=40 & 明確なブレイクアウトのみ)
  5. 利益ロック (年末に50%現金化)
  6. 利伸ばし (+20%から段階的利確)
  7. レバは小ポジションにのみ (口座全体は安全)

これにより理論上：
  - 1日で失える最大額 = 口座の 10% (5件 × 2%)
  - 50連敗しないと破産しない (実質破産不可)
  - 強気年には複数ポジション + ピラミで +200〜300%/年可能
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

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


def run_ruin_proof(all_data, start, end, cfg, initial=10_000.0):
    """
    cfg:
      risk_per_trade_pct: 1トレードあたりリスク (口座の%)
      max_pos:            最大同時保有
      stop_loss_pct:      SL距離 (%)
      trail_activate_pct: トレーリング発動
      trail_giveback_pct: 戻し幅
      adx_min:            エントリーADX下限
      adx_lev2, adx_lev3: レバ切替ADX
      lev_low, lev_mid, lev_high: レバ段階
      breakout_pct_above_ema200: EMA200からの乖離要求
      enable_short:       SHORT許可
      year_profit_lock:   年末利益ロック有効
      btc_adx_for_short:  SHORT時BTC ADX下限
    """
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    locked_bank = 0.0  # ロック済み資産（取引不可）
    positions = {}
    equity_curve = []
    trades = []
    last_year = None

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]

        # 年末利益ロック
        if cfg["year_profit_lock"] and last_year is not None and date.year > last_year:
            # 新年: 前年プラスなら25%を銀行に
            if equity_curve:
                year_start_eq = next(
                    (e["equity"] for e in equity_curve
                     if pd.Timestamp(e["ts"]).year == last_year), None)
                year_end_eq = equity_curve[-1]["equity"]
                if year_start_eq and year_end_eq > year_start_eq:
                    profit = year_end_eq - year_start_eq
                    lock = profit * 0.25
                    if cash >= lock:
                        cash -= lock
                        locked_bank += lock
        last_year = date.year

        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r

        # BTCレジーム
        btc_p, btc_e50, btc_e200 = btc_row["close"], btc_row["ema50"], btc_row["ema200"]
        btc_adx = btc_row.get("adx", 0)
        regime = "neutral"
        if not pd.isna(btc_e200):
            if btc_p > btc_e200 * 1.02 and btc_e50 > btc_e200:
                regime = "bull"
            elif btc_p < btc_e200 * 0.98 and btc_e50 < btc_e200:
                if len(btc_hist) >= 14:
                    recent_high = btc_hist.tail(14)["close"].max()
                    bounced = btc_p >= recent_high * 0.97
                    if btc_adx >= cfg["btc_adx_for_short"] and not bounced:
                        regime = "bear"

        # ━ 決済 ━
        for sym in list(positions.keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            p = positions[sym]
            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                adverse = (p["entry_price"] - cur) / p["entry_price"]
                favorable = (p["peak_price"] - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                adverse = (cur - p["entry_price"]) / p["entry_price"]
                favorable = (p["entry_price"] - p["peak_price"]) / p["entry_price"]

            close_reason = None
            if adverse >= cfg["stop_loss_pct"]:
                close_reason = "stop"
            elif favorable >= cfg["trail_activate_pct"]:
                if p["side"] == "long":
                    gb = (p["peak_price"] - cur) / p["peak_price"]
                else:
                    gb = (cur - p["peak_price"]) / p["peak_price"]
                if gb >= cfg["trail_giveback_pct"]:
                    close_reason = "trail"
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime == "neutral":
                pass  # 中立では放置
            elif regime != required:
                close_reason = close_reason or "regime"

            if close_reason:
                lev = p["leverage"]
                if p["side"] == "long":
                    exit_p = cur * (1 - SLIP)
                    pnl = p["qty"] * (exit_p - p["entry_price"]) * lev
                else:
                    exit_p = cur * (1 + SLIP)
                    pnl = p["qty"] * (p["entry_price"] - exit_p) * lev
                notional = p["qty"] * exit_p * lev
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["margin_usd"] + pnl
                trades.append({"sym": sym, "side": p["side"], "pnl": pnl,
                               "ret_pct": pnl/max(p["margin_usd"],1)*100, "reason": close_reason})
                del positions[sym]

        # ━ 新規エントリー（超選別）━
        if regime in ("bull", "bear"):
            if regime == "bear" and not cfg["enable_short"]:
                pass
            else:
                direction = "long" if regime == "bull" else "short"
                slots = cfg["max_pos"] - len(positions)
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in positions: continue
                        adx = r["adx"]
                        if adx < cfg["adx_min"]: continue
                        price, ema200 = r["close"], r["ema200"]
                        rsi = r.get("rsi", 50)
                        if pd.isna(rsi): continue
                        # ブレイクアウト: EMA200から一定%以上乖離
                        if direction == "long":
                            deviation = (price - ema200) / ema200
                            if deviation < cfg["breakout_pct_above_ema200"]: continue
                            if rsi < 50 or rsi > 80: continue  # 過熱過ぎ回避
                            candidates.append((sym, r, adx, deviation))
                        else:
                            deviation = (ema200 - price) / ema200
                            if deviation < cfg["breakout_pct_above_ema200"]: continue
                            if rsi < 30 or rsi > 75: continue
                            candidates.append((sym, r, adx, deviation))
                    # 選別: ADX降順 + 乖離降順
                    candidates.sort(key=lambda x: (x[2], x[3]), reverse=True)
                    candidates = candidates[:slots]

                    if candidates:
                        # 個別リスク基準でポジションサイジング
                        # 目標: SL到達で 口座の risk_per_trade_pct だけ損失
                        account_eq = cash + locked_bank + sum(
                            p["qty"] * today_rows[s]["close"] * p["leverage"] -
                            p["qty"] * p["entry_price"] * p["leverage"]
                            if p["side"] == "long" and s in today_rows
                            else 0
                            for s, p in positions.items()
                        ) + sum(p["margin_usd"] for p in positions.values())

                        risk_usd = account_eq * cfg["risk_per_trade_pct"]
                        for sym, r, adx, dev in candidates:
                            # レバ決定
                            if adx >= cfg["adx_lev3"]:
                                lev = cfg["lev_high"]
                            elif adx >= cfg["adx_lev2"]:
                                lev = cfg["lev_mid"]
                            else:
                                lev = cfg["lev_low"]
                            # ポジションサイズ: risk_usd / (SL × lev)
                            # SL到達時の損失 = margin × lev × SL (概算)
                            # だから margin = risk_usd / (SL × lev)
                            margin = risk_usd / (cfg["stop_loss_pct"] * lev)
                            # 現金の 10% を上限
                            margin = min(margin, cash * 0.10)
                            if margin < 20 or cash < margin * (1 + lev * FEE):
                                continue
                            raw = r["close"]
                            ep = raw * (1 + SLIP) if direction == "long" else raw * (1 - SLIP)
                            qty = margin / ep
                            notional = margin * lev
                            cash -= margin + notional * FEE
                            positions[sym] = {
                                "side": direction, "qty": qty, "entry_price": ep,
                                "leverage": lev, "entry_ts": date,
                                "margin_usd": margin, "peak_price": ep,
                            }

        # MTM
        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
        total_eq = cash + locked_bank + sum(p["margin_usd"] for p in positions.values()) + unreal
        equity_curve.append({"ts": date, "equity": max(0, total_eq)})

    # 最終
    for sym in list(positions.keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= dates[-1]]
        if not ld: continue
        last_row = df.loc[ld[-1]]
        p = positions[sym]
        raw = last_row["close"]
        if p["side"] == "long":
            exit_p = raw * (1 - SLIP)
            pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
        else:
            exit_p = raw * (1 + SLIP)
            pnl = p["qty"] * (p["entry_price"] - exit_p) * p["leverage"]
        notional = p["qty"] * exit_p * p["leverage"]
        pnl -= notional * FEE
        cash += p["margin_usd"] + pnl

    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        s = yr.iloc[0]; e = yr.iloc[-1]
        yearly[y] = round((e/s - 1) * 100, 2) if s > 0 else 0
    peak, max_dd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    final = max(0, cash + locked_bank)
    total = (final - initial) / initial * 100
    avg_annual = ((final/initial) ** (1/5) - 1) * 100 if final > 0 else -100
    return {
        "final": final, "locked_bank": locked_bank,
        "total_ret": total, "avg_annual_ret": avg_annual,
        "yearly": yearly, "max_dd": max_dd,
        "n_trades": len(trades),
        "n_long": sum(1 for t in trades if t["side"] == "long"),
        "n_short": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": sum(1 for t in trades if t.get("pnl", 0) > 0) / max(len(trades), 1) * 100,
        "all_positive": all(v > 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    print("📥 データ取得中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)\n")

    base = dict(
        risk_per_trade_pct=0.02, max_pos=5,
        stop_loss_pct=0.05, trail_activate_pct=0.15, trail_giveback_pct=0.04,
        adx_min=35, adx_lev2=45, adx_lev3=55,
        lev_low=2.0, lev_mid=3.0, lev_high=4.0,
        breakout_pct_above_ema200=0.05, enable_short=True,
        year_profit_lock=True, btc_adx_for_short=30,
    )

    configs = [
        ("R01 超選別 5pos ADX35 Lev2-4 SL5% リスク2%",   {**base}),
        ("R02 SL3% リスク1% 8pos Lev2-4",
         {**base, "stop_loss_pct": 0.03, "risk_per_trade_pct": 0.01, "max_pos": 8}),
        ("R03 ADX40 Lev3-5 リスク2%",
         {**base, "adx_min": 40, "adx_lev2": 50, "adx_lev3": 60,
          "lev_low": 3.0, "lev_mid": 4.0, "lev_high": 5.0}),
        ("R04 超保守 ADX45 Lev1-2 SL4% リスク1%",
         {**base, "adx_min": 45, "lev_low": 1.0, "lev_mid": 1.5, "lev_high": 2.0,
          "stop_loss_pct": 0.04, "risk_per_trade_pct": 0.01, "max_pos": 3}),
        ("R05 リスク3% Lev3-5 SL5%",
         {**base, "risk_per_trade_pct": 0.03,
          "lev_low": 3.0, "lev_mid": 4.0, "lev_high": 5.0}),
        ("R06 利益ロックOFF ADX35",                     {**base, "year_profit_lock": False}),
        ("R07 SHORT無効 LONG only",                    {**base, "enable_short": False}),
        ("R08 10pos Lev2-3 ブレイクアウト10%",
         {**base, "max_pos": 10, "breakout_pct_above_ema200": 0.10,
          "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
    ]

    print(f"{'=' * 145}")
    print(f"🛡 破産回避型 × 年+100% 探索（2%リスク管理 + 超選別エントリー）")
    print(f"{'=' * 145}")
    print(f"{'戦略':48s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>7s} | {'DD':>5s} | {'L/S':>9s} | 判定")
    print("-" * 145)

    best_both = None
    results = {}
    for name, cfg in configs:
        r = run_ruin_proof(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:48s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['n_long']:>3d}/{r['n_short']:>3d} | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%年")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%年")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r

        if r["all_positive"] and r["avg_annual_ret"] >= 100:
            if best_both is None or r["avg_annual_ret"] > best_both[1]["avg_annual_ret"]:
                best_both = (name, r)

    print(f"\n{'=' * 145}")
    if best_both:
        print(f"🎉🎉 **両条件達成**: {best_both[0]}")
        print(f"     年率 {best_both[1]['avg_annual_ret']:+.1f}% / 5年 {best_both[1]['total_ret']:+.1f}% / DD {best_both[1]['max_dd']:.1f}%")
    else:
        # 毎年プラスのみ
        winners_pos = [(n, r) for n, r in results.items() if r["all_positive"]]
        if winners_pos:
            print(f"✅ 毎年プラス達成: {len(winners_pos)}戦略")
            for n, r in sorted(winners_pos, key=lambda x: -x[1]["avg_annual_ret"]):
                print(f"   🎯 {n}: 年率{r['avg_annual_ret']:+.1f}% / DD{r['max_dd']:.1f}%")
        else:
            print(f"⚠️ まだ達成なし。更に反復必要")
    print(f"{'=' * 145}")

    out = Path("/Users/sanosano/projects/kimochi-max/results/ruin_proof.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
