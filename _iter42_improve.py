"""
Iter42: 2022年プラス化を目指す反復バックテスト
=====================================================
ACを土台に、案E/G/Hを組み合わせた8パターンを比較:
  - AC       : Iter41のベストライン
  - ACG1     : 案G — EMA50下抜け2日連続で確認
  - ACG2     : 案G変形 — EMA50から2%下で確認
  - ACE      : 案E — SHORT解禁
  - ACEG     : 案E + 案G1
  - ACH      : 案H — BTC ATRで動的レバ調整
  - ACEH     : 案E + 案H
  - ACEGH    : 全部入り
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi
import _legends_engine as LE

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")


def load_data():
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    if CACHE_PATH.exists():
        age_h = (time.time() - CACHE_PATH.stat().st_mtime) / 3600
        if age_h < 24:
            print(f"📦 キャッシュ使用（{age_h:.1f}h前）")
            with open(CACHE_PATH, "rb") as f:
                return pickle.load(f)
    print("📥 Binance実データ取得中...")
    d = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(d, f)
    return d


def i34_base_cfg():
    base = dict(
        risk_per_trade_pct=0.02, max_pos=20,
        stop_loss_pct=0.15,
        tp1_pct=0.10, tp1_fraction=0.4,
        tp2_pct=0.25, tp2_fraction=0.5,
        trail_activate_pct=0.30, trail_giveback_pct=0.08,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.0, lev_mid=1.0, lev_high=1.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
        max_margin_per_pos_pct=0.10,
    )
    return {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
            "max_pos": 12, "stop_loss_pct": 0.22,
            "tp1_pct": 0.10, "tp1_fraction": 0.25,
            "tp2_pct": 0.30, "tp2_fraction": 0.35,
            "trail_activate_pct": 0.50, "trail_giveback_pct": 0.15,
            "pyramid_enabled": True, "pyramid_max": 4,
            "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# エンジン（Iter42: 案E/G/H 対応）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_iter42(all_data, start, end, cfg, initial=10_000.0):
    """
    追加フラグ:
      btc_ema50_filter           : BTC<EMA50で新規エントリー停止
      btc_ema50_confirm_days     : 連続何日下抜けで確定か (案G1)
      btc_ema50_buffer           : 判定基準を厳しくする比率 (案G2, 例:0.02=2%)
      enable_short               : SHORT解禁 (案E)
      dynamic_leverage           : BTC ATR比率で動的にレバ調整 (案H)
    """
    FEE = LE.FEE; SLIP = LE.SLIP

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    if cfg.get("donchian_enabled"):
        n = cfg.get("donchian_n", 20)
        en = cfg.get("donchian_exit_n", 10)
        for sym in all_data:
            df = all_data[sym]
            if f"dch_h{n}" not in df.columns:
                df[f"dch_h{n}"] = df["high"].rolling(n).max().shift(1)
                df[f"dch_l{n}"] = df["low"].rolling(n).min().shift(1)
            if f"dch_h{en}" not in df.columns:
                df[f"dch_h{en}"] = df["high"].rolling(en).max().shift(1)
                df[f"dch_l{en}"] = df["low"].rolling(en).min().shift(1)

    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    state = {"cash": initial, "positions": {}, "locked_bank": 0.0}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    trades = []
    pending_entries = []
    last_year = None
    n_liquidations = 0
    n_short_trades = 0
    n_long_trades = 0
    btc_below_streak = 0  # 案G1
    lev_multiplier_history = []  # 案H の記録用

    for i, date in enumerate(dates):
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]

        if cfg.get("year_profit_lock") and last_year is not None and date.year > last_year:
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

        if pending_entries:
            for sym, direction, lev, margin, is_pyramid in pending_entries:
                if sym in today_rows:
                    r = today_rows[sym]
                    open_price = r.get("open", r["close"])
                    if is_pyramid:
                        if sym in state["positions"] and margin >= 20 and state["cash"] >= margin * (1 + lev * FEE):
                            LE._pyramid_add(state, sym, open_price, margin, date)
                    else:
                        if sym not in state["positions"]:
                            if margin < 20 or state["cash"] < margin * (1 + lev * FEE):
                                continue
                            LE._open_pos(state, sym, open_price, direction, lev, margin, date)
            pending_entries = []

        # ポジション管理
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            p = state["positions"][sym]
            high = r.get("high", r["close"])
            low = r.get("low", r["close"])
            liq_dist = LE.liquidation_distance(p["leverage"])
            if p["side"] == "long":
                adverse_intraday = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_intraday = (high - p["entry_price"]) / p["entry_price"]
            if adverse_intraday >= liq_dist:
                LE._close_pos(state, sym, r["close"], date, "liquidation",
                              trades, is_liquidation=True)
                n_liquidations += 1
                continue
            sl_pct = cfg["stop_loss_pct"]
            if p["side"] == "long":
                adverse_worst = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_worst = (high - p["entry_price"]) / p["entry_price"]
            if adverse_worst >= sl_pct:
                sl_price = (p["entry_price"] * (1 - sl_pct) if p["side"] == "long"
                            else p["entry_price"] * (1 + sl_pct))
                LE._close_pos(state, sym, sl_price, date, "stop_loss_intraday", trades)
                continue

            if cfg.get("donchian_enabled"):
                en = cfg.get("donchian_exit_n", 10)
                if p["side"] == "long" and low <= r.get(f"dch_l{en}", -1):
                    LE._close_pos(state, sym, r.get(f"dch_l{en}", r["close"]), date, "dch_exit", trades)
                    continue
                if p["side"] == "short" and high >= r.get(f"dch_h{en}", 1e18):
                    LE._close_pos(state, sym, r.get(f"dch_h{en}", r["close"]), date, "dch_exit", trades)
                    continue

            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                favorable = (cur - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                favorable = (p["entry_price"] - cur) / p["entry_price"]

            if cfg.get("pyramid_enabled") and p.get("pyramids", 0) < cfg.get("pyramid_max", 2):
                trigger = cfg.get("pyramid_trigger_pct", 0.10) * (p.get("pyramids", 0) + 1)
                if favorable >= trigger:
                    add_margin = p["margin_usd"] * cfg.get("pyramid_size_pct", 0.5) / (p.get("pyramids", 0) + 1)
                    add_margin = min(add_margin, state["cash"] * 0.05)
                    if add_margin >= 20:
                        pending_entries.append((sym, p["side"], p["leverage"], add_margin, True))

            partial = p.get("partial_taken", 0)
            if partial == 0 and favorable >= cfg["tp1_pct"]:
                LE._close_pos(state, sym, cur, date, "tp1", trades, qty_fraction=cfg["tp1_fraction"])
                if sym not in state["positions"]: continue
            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if p_again.get("partial_taken", 0) == 1 and favorable >= cfg["tp2_pct"]:
                LE._close_pos(state, sym, cur, date, "tp2", trades, qty_fraction=cfg["tp2_fraction"])
                if sym not in state["positions"]: continue

            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if p_again["side"] == "long":
                fav_p = (p_again["peak_price"] - p_again["entry_price"]) / p_again["entry_price"]
                gb = (p_again["peak_price"] - cur) / p_again["peak_price"]
            else:
                fav_p = (p_again["entry_price"] - p_again["peak_price"]) / p_again["entry_price"]
                gb = (cur - p_again["peak_price"]) / p_again["peak_price"]
            if fav_p >= cfg["trail_activate_pct"] and gb >= cfg["trail_giveback_pct"]:
                LE._close_pos(state, sym, cur, date, "trail", trades)

        # BTCレジーム判定
        btc_p, btc_e50, btc_e200 = btc_row["close"], btc_row["ema50"], btc_row["ema200"]
        btc_adx = btc_row.get("adx", 0)
        btc_atr = btc_row.get("atr", 0)
        regime = "neutral"
        if not pd.isna(btc_e200):
            if btc_p > btc_e200 * 1.02 and btc_e50 > btc_e200:
                regime = "bull"
            elif btc_p < btc_e200 * 0.98 and btc_e50 < btc_e200:
                if len(btc_hist) >= 14:
                    recent_high = btc_hist.tail(14)["close"].max()
                    if btc_adx >= cfg.get("btc_adx_for_short", 40) and btc_p < recent_high * 0.97:
                        regime = "bear"

        # ━★ 案G: EMA50下抜けカウント（連続日数）
        if not pd.isna(btc_e50) and btc_p < btc_e50:
            btc_below_streak += 1
        else:
            btc_below_streak = 0

        # ━★ 案G: フィルタ適用
        entry_regime = regime
        if cfg.get("btc_ema50_filter") and regime == "bull":
            confirm_days = cfg.get("btc_ema50_confirm_days", 1)
            buffer = cfg.get("btc_ema50_buffer", 0.0)
            if not pd.isna(btc_e50):
                threshold = btc_e50 * (1 - buffer)
                if btc_p < threshold and btc_below_streak >= confirm_days:
                    entry_regime = "neutral"

        # ━★ 案H: BTC ATR による動的レバ
        lev_multiplier = 1.0
        if cfg.get("dynamic_leverage") and btc_p > 0 and btc_atr > 0:
            vol_ratio = btc_atr / btc_p
            if vol_ratio > 0.07:
                lev_multiplier = 0.4
            elif vol_ratio > 0.05:
                lev_multiplier = 0.6
            elif vol_ratio > 0.04:
                lev_multiplier = 0.8
        lev_multiplier_history.append(lev_multiplier)

        # レジーム離脱で決済
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            p = state["positions"][sym]
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime != "neutral":
                LE._close_pos(state, sym, today_rows[sym]["close"], date, "regime", trades)

        # エントリー
        if entry_regime in ("bull", "bear") and i < len(dates) - 1:
            if entry_regime == "bear" and not cfg.get("enable_short"):
                pass
            else:
                direction = "long" if entry_regime == "bull" else "short"
                slots = cfg["max_pos"] - len(state["positions"])
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in state["positions"]: continue
                        adx = r["adx"]
                        if adx < cfg["adx_min"]: continue
                        price, ema200 = r["close"], r["ema200"]
                        rsi = r.get("rsi", 50)
                        atr = r.get("atr", 0)
                        if pd.isna(rsi): continue
                        if cfg.get("donchian_enabled"):
                            n = cfg.get("donchian_n", 20)
                            dch_h = r.get(f"dch_h{n}")
                            dch_l = r.get(f"dch_l{n}")
                            if pd.isna(dch_h) or pd.isna(dch_l): continue
                            if direction == "long" and r["high"] < dch_h: continue
                            if direction == "short" and r["low"] > dch_l: continue
                        if cfg.get("vcp_enabled"):
                            vol_pct = atr / price if price > 0 else 1
                            if vol_pct > cfg.get("vcp_atr_max_pct", 0.06): continue
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
                            # ★ 案H: 動的レバ適用
                            lev = max(1.0, lev * lev_multiplier)
                            margin = risk_usd / (cfg["stop_loss_pct"] * lev)
                            margin = min(margin, state["cash"] * cfg.get("max_margin_per_pos_pct", 0.10))
                            if margin < 20: continue
                            pending_entries.append((sym, direction, lev, margin, False))

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
        LE._close_pos(state, sym, df.loc[ld[-1], "close"], final_date, "final", trades)
    final = state["cash"] + state["locked_bank"]
    equity_curve[-1] = {"ts": final_date, "equity": final}

    # 集計
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
    avg_annual = ((final / initial) ** (1/5) - 1) * 100 if final > 0 else -100

    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    n_long = sum(1 for t in trades if t["side"] == "long")
    n_short = sum(1 for t in trades if t["side"] == "short")
    short_wins = sum(1 for t in trades if t["side"] == "short" and t.get("pnl", 0) > 0)
    short_pnl = sum(t.get("pnl", 0) for t in trades if t["side"] == "short")

    eq_weekly = eq_df.resample("W").last().dropna()
    eq_list = [{"ts": str(d)[:10], "equity": round(float(e), 2)}
               for d, e in eq_weekly["equity"].items()]

    return {
        "final": round(final, 2),
        "total_ret": round(total, 2),
        "avg_annual_ret": round(avg_annual, 2),
        "yearly": yearly,
        "max_dd": round(max_dd, 2),
        "n_trades": len(trades),
        "n_liquidations": n_liquidations,
        "n_long": n_long,
        "n_short": n_short,
        "short_wins": short_wins,
        "short_pnl": round(short_pnl, 2),
        "win_rate": round(wins / max(len(trades), 1) * 100, 2),
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
        "equity_weekly": eq_list,
        "avg_lev_multiplier": round(np.mean(lev_multiplier_history), 3) if lev_multiplier_history else 1.0,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BTC年別リターン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def compute_btc_yearly(btc_df):
    """BTC自体の年別リターンを計算"""
    yearly = {}
    prev_close = None
    for y in range(2020, 2025):
        df = btc_df[btc_df.index.year == y]
        if len(df) == 0: continue
        start_close = df["close"].iloc[0]
        end_close = df["close"].iloc[-1]
        base = prev_close if prev_close else start_close
        yearly[y] = {
            "start": round(float(base), 2),
            "end": round(float(end_close), 2),
            "return_pct": round((end_close / base - 1) * 100, 2),
        }
        prev_close = end_close
    return yearly


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# パターン定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_variants():
    b = i34_base_cfg()
    ac = {**b, "pyramid_max": 2, "btc_ema50_filter": True}  # Iter41のAC
    variants = [
        ("AC (Iter41ベース)",         ac),
        ("ACG1 (EMA50 2日確認)",     {**ac, "btc_ema50_confirm_days": 2}),
        ("ACG2 (EMA50 -2%バッファ)",  {**ac, "btc_ema50_buffer": 0.02}),
        ("ACE (SHORT解禁)",           {**ac, "enable_short": True}),
        ("ACEG (SHORT+G1)",          {**ac, "enable_short": True, "btc_ema50_confirm_days": 2}),
        ("ACH (動的レバ)",             {**ac, "dynamic_leverage": True}),
        ("ACEH (SHORT+動的レバ)",      {**ac, "enable_short": True, "dynamic_leverage": True}),
        ("ACEGH (全部入り)",            {**ac, "enable_short": True, "btc_ema50_confirm_days": 2,
                                          "dynamic_leverage": True}),
    ]
    return variants


def tag(r):
    t = []
    if r["all_positive"]: t.append("🎯毎年+")
    elif r["no_negative"]: t.append("🟢ﾏｲﾅｽ無")
    if r["avg_annual_ret"] >= 70: t.append("🚀+70%")
    elif r["avg_annual_ret"] >= 50: t.append("⭐+50%")
    elif r["avg_annual_ret"] >= 30: t.append("💪+30%")
    if r["max_dd"] < 40: t.append("🛡DD<40")
    if r["n_liquidations"] == 0: t.append("✅清算0")
    return " ".join(t)


def main():
    print("=" * 120)
    print("🎯 Iter42: 2022年プラス化を狙う 8パターン比較")
    print("=" * 120)
    all_data = load_data()
    btc_yearly = compute_btc_yearly(all_data["BTC/USDT"])

    print("\n📊 BTC自体の年別推移:")
    for y, v in btc_yearly.items():
        print(f"   {y}: ${v['start']:>8,.0f} → ${v['end']:>8,.0f}  "
              f"({'+' if v['return_pct']>=0 else ''}{v['return_pct']:>6.1f}%)")

    variants = build_variants()
    print(f"\n{'No':3s} | {'戦略':30s} | {'20':>5s} {'21':>5s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'L':>4s} {'S':>3s} | {'清算':>3s} | 判定")
    print("-" * 135)
    results = {}
    for i, (name, cfg) in enumerate(variants, 1):
        t0 = time.time()
        r = run_iter42(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{i:3d} | {name:30s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+4.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['n_long']:>4d} {r['n_short']:>3d} | {r['n_liquidations']:>3d} | "
        row += tag(r)
        print(row, flush=True)
        results[name] = r

    # 相関計算: 各戦略の年別リターン vs BTC年別リターン
    correlations = {}
    btc_rets = [btc_yearly[y]["return_pct"] for y in sorted(btc_yearly.keys())]
    for name, r in results.items():
        strat_rets = [r["yearly"].get(y, 0) for y in sorted(btc_yearly.keys())]
        if len(strat_rets) == len(btc_rets) and len(strat_rets) > 1:
            correlations[name] = float(np.corrcoef(btc_rets, strat_rets)[0, 1])

    positives = [(n, r) for n, r in results.items() if r["no_negative"]]
    best_pos = max(positives, key=lambda x: x[1]["avg_annual_ret"]) if positives else None
    best_2022 = max(results.items(), key=lambda x: x[1]["yearly"].get(2022, -999))

    print("\n" + "=" * 120)
    if best_pos:
        n, r = best_pos
        print(f"🏆 毎年プラス達成: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / "
              f"$10K→${r['final']:,.0f} / LONG{r['n_long']}件 SHORT{r['n_short']}件")
    n, r = best_2022
    print(f"🥇 2022年最良: {n} (2022 {r['yearly'].get(2022, 0):+.2f}%)")
    print("=" * 120)

    out_path = (Path(__file__).resolve().parent / "results" / "iter42_improve.json")
    out_path.write_text(json.dumps({
        "results": results,
        "btc_yearly": btc_yearly,
        "correlations": correlations,
        "best_no_negative": best_pos[0] if best_pos else None,
        "best_2022": best_2022[0],
    }, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out_path}")


if __name__ == "__main__":
    main()
