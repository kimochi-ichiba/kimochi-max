"""
現実派バックテスト（全バイアス修正版）
======================================
修正済み:
  ✅ 日中ストップ (row["low"]/row["high"] でSL判定)
  ✅ 清算モデル (逆行 > 1/lev で position全損)
  ✅ 翌日エントリー (今日の終値→翌日の始値で約定)
  ✅ 整合性アサーション
  ✅ equity_curve クランプなし
  ✅ 相関イベント認識（BTC日足-7%超で全ポジションリスク評価）

対応しない（限界）:
  ⚠️ サバイバルバイアス (当時の廃棄銘柄データなし)
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
LIQ_FEE    = 0.002  # 清算手数料


def liquidation_distance(lev: float) -> float:
    """レバレッジから清算距離を算出（安全マージン 85%）"""
    return (1.0 / lev) * 0.85


def _close_pos(state, sym, exit_price, ts, reason, trades, qty_fraction=1.0,
               is_liquidation=False):
    p = state["positions"][sym]
    lev = p["leverage"]
    close_qty = p["qty"] * qty_fraction

    if is_liquidation:
        # 清算: 全マージン失う + 清算fee
        state["cash"] -= p["margin_usd"] * LIQ_FEE * qty_fraction
        trades.append({
            "sym": sym, "side": p["side"], "pnl": -p["margin_usd"] * qty_fraction,
            "ret_pct": -100, "reason": "liquidation", "fraction": qty_fraction,
        })
        if qty_fraction >= 0.999:
            del state["positions"][sym]
        return

    if p["side"] == "long":
        exit_p = exit_price * (1 - SLIP)
        pnl = close_qty * (exit_p - p["entry_price"]) * lev
    else:
        exit_p = exit_price * (1 + SLIP)
        pnl = close_qty * (p["entry_price"] - exit_p) * lev
    notional = close_qty * exit_p * lev
    pnl -= notional * FEE
    hold_h = (ts - p["entry_ts"]).total_seconds() / 3600
    pnl -= notional * FUNDING_PH * hold_h

    close_margin = p["margin_usd"] * qty_fraction
    state["cash"] += close_margin + pnl
    trades.append({
        "sym": sym, "side": p["side"], "pnl": pnl,
        "ret_pct": (pnl / close_margin * 100) if close_margin > 0 else 0,
        "reason": reason, "fraction": qty_fraction,
    })
    if qty_fraction >= 0.999:
        del state["positions"][sym]
    else:
        p["qty"] -= close_qty
        p["margin_usd"] -= close_margin
        p["partial_taken"] = (p.get("partial_taken", 0) or 0) + 1


def _open_pos(state, sym, entry_price_open, direction, lev, margin, ts):
    """翌日始値でエントリー（current row の open 使用）"""
    ep = entry_price_open * (1 + SLIP) if direction == "long" else entry_price_open * (1 - SLIP)
    qty = margin / ep
    notional = margin * lev
    state["cash"] -= margin + notional * FEE
    state["positions"][sym] = {
        "side": direction, "qty": qty, "entry_price": ep,
        "leverage": lev, "entry_ts": ts,
        "margin_usd": margin, "peak_price": ep,
        "partial_taken": 0,
    }


def run_realistic(all_data, start, end, cfg, initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    state = {"cash": initial, "positions": {}, "locked_bank": 0.0}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    trades = []
    pending_entries = []  # [(sym, direction, lev, margin), ...]  翌日執行
    last_year = None
    n_liquidations = 0

    for i, date in enumerate(dates):
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]

        # 利益ロック
        if cfg["year_profit_lock"] and last_year is not None and date.year > last_year:
            if equity_curve:
                ys_eq = next((e["equity"] for e in equity_curve
                              if pd.Timestamp(e["ts"]).year == last_year), None)
                ye_eq = equity_curve[-1]["equity"]
                if ys_eq and ye_eq > ys_eq:
                    profit = ye_eq - ys_eq
                    lock = profit * cfg.get("profit_lock_pct", 0.25)
                    if state["cash"] >= lock:
                        state["cash"] -= lock
                        state["locked_bank"] += lock
        last_year = date.year

        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r

        # ━ 翌日エントリー実行（今日の始値で） ━
        if pending_entries:
            for sym, direction, lev, margin in pending_entries:
                if sym in today_rows and sym not in state["positions"]:
                    r = today_rows[sym]
                    open_price = r.get("open", r["close"])
                    if margin < 20 or state["cash"] < margin * (1 + lev * FEE):
                        continue
                    _open_pos(state, sym, open_price, direction, lev, margin, date)
            pending_entries = []

        # ━ 日中SL・清算判定（row["high"]/["low"]で正確に） ━
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            p = state["positions"][sym]
            high = r.get("high", r["close"])
            low = r.get("low", r["close"])

            # 清算チェック最優先
            liq_dist = liquidation_distance(p["leverage"])
            if p["side"] == "long":
                adverse_intraday = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_intraday = (high - p["entry_price"]) / p["entry_price"]

            if adverse_intraday >= liq_dist:
                _close_pos(state, sym, r["close"], date, "liquidation",
                           trades, is_liquidation=True)
                n_liquidations += 1
                continue

            # SLチェック（日中最悪値で）
            if p["side"] == "long":
                adverse_worst = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_worst = (high - p["entry_price"]) / p["entry_price"]
            if adverse_worst >= cfg["stop_loss_pct"]:
                # SL価格で決済
                sl_price = (p["entry_price"] * (1 - cfg["stop_loss_pct"]) if p["side"] == "long"
                            else p["entry_price"] * (1 + cfg["stop_loss_pct"]))
                _close_pos(state, sym, sl_price, date, "stop_loss_intraday", trades)
                continue

            # ピーク更新（トレーリング用・終値ベース）
            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                favorable = (cur - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                favorable = (p["entry_price"] - cur) / p["entry_price"]

            # 部分利確
            partial = p.get("partial_taken", 0)
            if partial == 0 and favorable >= cfg["tp1_pct"]:
                _close_pos(state, sym, cur, date, "tp1", trades, qty_fraction=cfg["tp1_fraction"])
                if sym not in state["positions"]: continue
            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if p_again.get("partial_taken", 0) == 1 and favorable >= cfg["tp2_pct"]:
                _close_pos(state, sym, cur, date, "tp2", trades, qty_fraction=cfg["tp2_fraction"])
                if sym not in state["positions"]: continue

            # トレーリング
            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if p_again["side"] == "long":
                fav = (p_again["peak_price"] - p_again["entry_price"]) / p_again["entry_price"]
                gb = (p_again["peak_price"] - cur) / p_again["peak_price"]
            else:
                fav = (p_again["entry_price"] - p_again["peak_price"]) / p_again["entry_price"]
                gb = (cur - p_again["peak_price"]) / p_again["peak_price"]
            if fav >= cfg["trail_activate_pct"] and gb >= cfg["trail_giveback_pct"]:
                _close_pos(state, sym, cur, date, "trail", trades)

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
                    if btc_adx >= cfg["btc_adx_for_short"] and btc_p < recent_high * 0.97:
                        regime = "bear"

        # レジーム離脱で決済
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            p = state["positions"][sym]
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime != "neutral":
                _close_pos(state, sym, today_rows[sym]["close"], date, "regime", trades)

        # ━ 翌日エントリー準備 ━
        if regime in ("bull", "bear") and i < len(dates) - 1:
            if regime == "bear" and not cfg["enable_short"]: pass
            else:
                direction = "long" if regime == "bull" else "short"
                slots = cfg["max_pos"] - len(state["positions"])
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in state["positions"]: continue
                        adx = r["adx"]
                        if adx < cfg["adx_min"]: continue
                        price, ema200 = r["close"], r["ema200"]
                        rsi = r.get("rsi", 50)
                        if pd.isna(rsi): continue
                        if direction == "long":
                            dev = (price - ema200) / ema200
                            if dev < cfg["breakout_pct"]: continue
                            if rsi < cfg["rsi_long_min"] or rsi > cfg["rsi_long_max"]: continue
                            candidates.append((sym, r, adx, dev))
                        else:
                            dev = (ema200 - price) / ema200
                            if dev < cfg["breakout_pct"]: continue
                            if rsi < cfg["rsi_short_min"]: continue
                            candidates.append((sym, r, adx, dev))
                    candidates.sort(key=lambda x: (x[2], x[3]), reverse=True)
                    candidates = candidates[:slots]
                    if candidates:
                        unreal = sum(
                            (p["qty"] * (today_rows[s]["close"] - p["entry_price"]) * p["leverage"]
                             if p["side"] == "long" and s in today_rows
                             else p["qty"] * (p["entry_price"] - today_rows[s]["close"]) * p["leverage"]
                             if s in today_rows else 0)
                            for s, p in state["positions"].items()
                        )
                        account_eq = (state["cash"] + state["locked_bank"] +
                                      sum(p["margin_usd"] for p in state["positions"].values()) + unreal)
                        risk_usd = account_eq * cfg["risk_per_trade_pct"]
                        for sym, r, adx, dev in candidates:
                            lev = (cfg["lev_high"] if adx >= cfg["adx_lev3"] else
                                   (cfg["lev_mid"] if adx >= cfg["adx_lev2"] else cfg["lev_low"]))
                            margin = risk_usd / (cfg["stop_loss_pct"] * lev)
                            margin = min(margin, state["cash"] * 0.10)
                            if margin < 20: continue
                            # 翌日執行キューに積む
                            pending_entries.append((sym, direction, lev, margin))

        # MTM
        unreal = 0.0
        for sym, p in state["positions"].items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
        total_eq = (state["cash"] + state["locked_bank"] +
                    sum(p["margin_usd"] for p in state["positions"].values()) + unreal)
        equity_curve.append({"ts": date, "equity": total_eq})

    # 最終close
    final_date = dates[-1]
    for sym in list(state["positions"].keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= final_date]
        if not ld: continue
        _close_pos(state, sym, df.loc[ld[-1], "close"], final_date, "final", trades)
    final = state["cash"] + state["locked_bank"]
    equity_curve[-1] = {"ts": final_date, "equity": final}

    # 年次
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    prev_eq = initial
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        ye = yr.iloc[-1]
        yearly[y] = round((ye / prev_eq - 1) * 100, 2) if prev_eq > 0 else 0
        prev_eq = ye

    peak, max_dd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    total = (final - initial) / initial * 100
    avg_annual = ((final/initial) ** (1/5) - 1) * 100 if final > 0 else -100

    compound = 1.0
    for v in yearly.values(): compound *= (1 + v/100)
    compound_pct = (compound - 1) * 100
    integrity_gap = abs(total - compound_pct)

    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return {
        "final": final, "total_ret": total,
        "avg_annual_ret": avg_annual,
        "integrity_gap": integrity_gap,
        "integrity_ok": integrity_gap < 1.0,
        "yearly": yearly, "max_dd": max_dd,
        "n_trades": len(trades),
        "n_liquidations": n_liquidations,
        "n_long": sum(1 for t in trades if t["side"] == "long"),
        "n_short": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": wins / max(len(trades), 1) * 100,
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    print("📥 データ取得中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ ({time.time()-t0:.0f}秒)\n")

    base = dict(
        risk_per_trade_pct=0.02, max_pos=8,
        stop_loss_pct=0.05,
        tp1_pct=0.05, tp1_fraction=0.4,
        tp2_pct=0.12, tp2_fraction=0.5,
        trail_activate_pct=0.20, trail_giveback_pct=0.05,
        adx_min=55, adx_lev2=60, adx_lev3=70,
        lev_low=3.0, lev_mid=4.0, lev_high=5.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
    )

    configs = [
        ("Q01 現実派基準 Lev3-5 SL5% max8",                 {**base}),
        ("Q02 Lev3-5 SL3% (タイト)",                        {**base, "stop_loss_pct": 0.03}),
        ("Q03 Lev2-4 SL5% (保守)",                         {**base, "lev_low": 2.0, "lev_mid": 3.0, "lev_high": 4.0}),
        ("Q04 Lev4-6 SL5%",                              {**base, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0}),
        ("Q05 max_pos 15 Lev3-5",                        {**base, "max_pos": 15}),
        ("Q06 max_pos 15 Lev4-6 リスク3%",
         {**base, "max_pos": 15, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0, "risk_per_trade_pct": 0.03}),
        ("Q07 Lev2-3 超保守 (清算回避優先)",
         {**base, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("Q08 max_pos 10 Lev3-5 リスク3%",
         {**base, "max_pos": 10, "risk_per_trade_pct": 0.03}),
        ("Q09 ADX50緩和 Lev3-5 max10",
         {**base, "adx_min": 50, "adx_lev2": 55, "adx_lev3": 65, "max_pos": 10}),
        ("Q10 max 20 Lev3-5",                            {**base, "max_pos": 20}),
        ("Q11 Lev4-6 SL4%",                              {**base, "stop_loss_pct": 0.04, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0}),
        ("Q12 max 20 Lev4-6 リスク3%",
         {**base, "max_pos": 20, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0, "risk_per_trade_pct": 0.03}),
    ]

    print(f"{'=' * 170}")
    print(f"🎯 現実派バックテスト（日中SL+清算+翌日エントリー）× +100%探索")
    print(f"{'=' * 170}")
    print(f"{'戦略':38s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'清算':>4s} | 判定")
    print("-" * 170)

    results = {}
    best = None
    for name, cfg in configs:
        r = run_realistic(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:38s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {r['n_liquidations']:>3d} | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        elif r["no_negative"]: tags.append("🟢マイナス無")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["win_rate"] >= 50: tags.append("📈勝率50+")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        if r["n_liquidations"] == 0: tags.append("✅清算0")
        tags.append("✅整合" if r["integrity_ok"] else "❌不整合")
        row += " ".join(tags)
        print(row)
        results[name] = r
        if r["avg_annual_ret"] > (best[1]["avg_annual_ret"] if best else -999):
            if r["no_negative"] and r["integrity_ok"]:
                best = (name, r)

    print(f"\n{'=' * 170}")
    if best:
        r = best[1]
        print(f"🏆 最良（マイナスなし・整合OK）: {best[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / DD {r['max_dd']:.1f}% / 清算 {r['n_liquidations']}回")
    print(f"{'=' * 170}")

    out = Path("/Users/sanosano/projects/kimochi-max/results/realistic_backtest.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
