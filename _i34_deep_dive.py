"""
I34 深掘り分析スクリプト
================================
目的:
  - I34（Livermore完全 Lev2.5 ピラミ4）を日付つきで再実行
  - 2024年がマイナスになった原因をトレード単位で特定
  - HTMLレポート用のJSONデータを出力
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

# ━━━━━ エンジンを一時的に計装 (モンキーパッチ) ━━━━━
_orig_close = LE._close_pos
_orig_open = LE._open_pos
_orig_pyr = LE._pyramid_add

def _close_patched(state, sym, exit_price, ts, reason, trades, qty_fraction=1.0,
                   is_liquidation=False):
    before_len = len(trades)
    p_before = state["positions"].get(sym, {}).copy() if sym in state["positions"] else None
    _orig_close(state, sym, exit_price, ts, reason, trades, qty_fraction, is_liquidation)
    # 追加情報を最後のtradeに書き込む
    if len(trades) > before_len:
        t = trades[-1]
        t["ts"] = ts
        t["sym"] = sym
        t["exit_price"] = float(exit_price)
        if p_before:
            t["entry_ts"] = p_before.get("entry_ts")
            t["entry_price"] = float(p_before.get("entry_price", 0))
            t["leverage"] = p_before.get("leverage", 1)
            t["pyramids"] = p_before.get("pyramids", 0)
            if t.get("entry_ts"):
                t["hold_days"] = (ts - t["entry_ts"]).total_seconds() / 86400

LE._close_pos = _close_patched

# ━━━━━ データロード ━━━━━
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


# ━━━━━ I34 設定 ━━━━━
def i34_config():
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


# ━━━━━ run_legends から trades も引っ張るために再実装（コピー改変） ━━━━━
def run_with_trades(all_data, start, end, cfg, initial=10_000.0):
    """run_legends を元に、trades・full equity_curve もそのまま返す版"""
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
            if "vol_ma20" not in df.columns:
                df["vol_ma20"] = df["volume"].rolling(20).mean()

    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    # run_legends を呼ぶが、完全な equity_curve を得るため内部を再生
    # 簡略化: run_legends を使うとサンプリングされるので、ここでは自前ループ不要。
    # 代わりに、equity_curve は run_legends の7日サンプリング結果を使い、
    # trades には ts 情報が乗る（モンキーパッチ済み）。
    res = LE.run_legends(all_data, start, end, cfg, initial=initial)

    # 今回は run_legends 内部の equity と trades が直接取れないので、
    # ここでもう一度ループを回す代わりに、res の equity を返す
    return res


# ━━━━━ run_legends 自体を書き換えて trades も返す版 ━━━━━
def run_legends_full(all_data, start, end, cfg, initial=10_000.0):
    """run_legends をコピー+改変し、trades と daily equity を返す"""
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

    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    state = {"cash": initial, "positions": {}, "locked_bank": 0.0}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    trades = []
    pending_entries = []
    last_year = None
    n_liquidations = 0

    FEE = LE.FEE; SLIP = LE.SLIP; FUNDING_PH = LE.FUNDING_PH; LIQ_FEE = LE.LIQ_FEE

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
            if cfg.get("tp3_pct") and p_again.get("partial_taken", 0) == 2 and favorable >= cfg["tp3_pct"]:
                LE._close_pos(state, sym, cur, date, "tp3", trades, qty_fraction=cfg.get("tp3_fraction", 0.5))
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

        btc_p, btc_e50, btc_e200 = btc_row["close"], btc_row["ema50"], btc_row["ema200"]
        btc_adx = btc_row.get("adx", 0)
        regime = "neutral"
        if not pd.isna(btc_e200):
            if btc_p > btc_e200 * 1.02 and btc_e50 > btc_e200:
                regime = "bull"
            elif btc_p < btc_e200 * 0.98 and btc_e50 < btc_e200:
                if len(btc_hist) >= 14:
                    recent_high = btc_hist.tail(14)["close"].max()
                    if btc_adx >= cfg.get("btc_adx_for_short", 40) and btc_p < recent_high * 0.97:
                        regime = "bear"

        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            p = state["positions"][sym]
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime != "neutral":
                LE._close_pos(state, sym, today_rows[sym]["close"], date, "regime", trades)

        if regime in ("bull", "bear") and i < len(dates) - 1:
            if regime == "bear" and not cfg.get("enable_short"):
                pass
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
                        if cfg.get("volume_confirm"):
                            vma = r.get("vol_ma20", 0)
                            if vma > 0 and r.get("volume", 0) < vma * cfg.get("volume_mult", 1.5): continue
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
                            margin = min(margin, state["cash"] * cfg.get("max_margin_per_pos_pct", 0.10))
                            if margin < 20: continue
                            pending_entries.append((sym, direction, lev, margin, False))

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

    final_date = dates[-1]
    for sym in list(state["positions"].keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= final_date]
        if not ld: continue
        LE._close_pos(state, sym, df.loc[ld[-1], "close"], final_date, "final", trades)
    final = state["cash"] + state["locked_bank"]
    equity_curve[-1] = {"ts": final_date, "equity": final}

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "n_liquidations": n_liquidations,
        "final": final,
    }


# ━━━━━ main ━━━━━
def main():
    print("=" * 70)
    print("I34 深掘り分析")
    print("=" * 70)

    all_data = load_data()
    cfg = i34_config()

    print("\n🔬 I34を日付つきで再実行中...")
    t0 = time.time()
    res = run_legends_full(all_data, "2020-01-01", "2024-12-31", cfg)
    print(f"✅ 完了 ({time.time() - t0:.0f}秒)")
    print(f"   取引数: {len(res['trades'])}, 清算: {res['n_liquidations']}, 最終: ${res['final']:,.0f}")

    # ━━━━━ equity を日次DataFrameに ━━━━━
    eq_df = pd.DataFrame(res["equity_curve"])
    eq_df["ts"] = pd.to_datetime(eq_df["ts"])
    eq_df = eq_df.set_index("ts").sort_index()

    # 月次集計
    monthly = []
    prev_eq = 10000.0
    for period, group in eq_df.groupby(pd.Grouper(freq="M")):
        if len(group) == 0: continue
        end_eq = group["equity"].iloc[-1]
        ret_pct = (end_eq / prev_eq - 1) * 100 if prev_eq > 0 else 0
        monthly.append({
            "month": period.strftime("%Y-%m"),
            "year": period.year,
            "end_equity": round(end_eq, 2),
            "ret_pct": round(ret_pct, 2),
        })
        prev_eq = end_eq

    # 年別集計（start vs end equity）
    yearly = {}
    prev_eq = 10000.0
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        ye = yr.iloc[-1]
        yearly[y] = {
            "start_equity": round(prev_eq, 2),
            "end_equity": round(ye, 2),
            "ret_pct": round((ye / prev_eq - 1) * 100, 2) if prev_eq > 0 else 0,
            "peak_equity": round(yr.max(), 2),
            "trough_equity": round(yr.min(), 2),
        }
        prev_eq = ye

    # ━━━━━ トレード詳細 ━━━━━
    trades_clean = []
    for t in res["trades"]:
        ts = t.get("ts")
        ent = t.get("entry_ts")
        trades_clean.append({
            "exit_ts": str(ts)[:10] if ts is not None else "",
            "entry_ts": str(ent)[:10] if ent is not None else "",
            "year": ts.year if ts is not None else 0,
            "sym": t.get("sym", ""),
            "side": t.get("side", ""),
            "pnl": round(float(t.get("pnl", 0)), 2),
            "ret_pct": round(float(t.get("ret_pct", 0)), 2),
            "reason": t.get("reason", ""),
            "fraction": round(float(t.get("fraction", 1.0)), 2),
            "hold_days": round(float(t.get("hold_days", 0)), 1),
            "leverage": float(t.get("leverage", 0)),
            "pyramids": int(t.get("pyramids", 0)),
        })

    # 2024 のトレードのみ
    trades_2024 = [t for t in trades_clean if t["year"] == 2024]
    # pnl ソート（損失大きい順）
    trades_2024_sorted = sorted(trades_2024, key=lambda x: x["pnl"])

    # 2024年の原因分析用集計
    reason_2024 = {}
    for t in trades_2024:
        r = t["reason"]
        if r not in reason_2024:
            reason_2024[r] = {"count": 0, "pnl": 0, "wins": 0, "losses": 0}
        reason_2024[r]["count"] += 1
        reason_2024[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0: reason_2024[r]["wins"] += 1
        else: reason_2024[r]["losses"] += 1
    for r in reason_2024:
        reason_2024[r]["pnl"] = round(reason_2024[r]["pnl"], 2)

    # 2024年のシンボル別集計（ワースト銘柄）
    sym_2024 = {}
    for t in trades_2024:
        s = t["sym"]
        if s not in sym_2024:
            sym_2024[s] = {"count": 0, "pnl": 0}
        sym_2024[s]["count"] += 1
        sym_2024[s]["pnl"] += t["pnl"]
    sym_2024_list = sorted([
        {"sym": s, "count": v["count"], "pnl": round(v["pnl"], 2)}
        for s, v in sym_2024.items()
    ], key=lambda x: x["pnl"])

    # 2024 月別清算数と大損失
    month_2024 = {}
    for t in trades_2024:
        m = t["exit_ts"][:7]
        if m not in month_2024:
            month_2024[m] = {"trades": 0, "liq": 0, "pnl": 0, "big_losses": 0}
        month_2024[m]["trades"] += 1
        month_2024[m]["pnl"] += t["pnl"]
        if t["reason"] == "liquidation":
            month_2024[m]["liq"] += 1
        if t["pnl"] < -200:
            month_2024[m]["big_losses"] += 1
    month_2024_list = [
        {"month": m, **{k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()}}
        for m, d in sorted(month_2024.items())
    ]

    # BTC 2024 価格
    btc_df = all_data["BTC/USDT"]
    btc_2024 = btc_df[(btc_df.index >= "2024-01-01") & (btc_df.index <= "2024-12-31")]
    btc_prices = [
        {"ts": str(d)[:10], "close": round(float(p), 2)}
        for d, p in btc_2024["close"].items()
    ]

    # equity curve 全期間（週次サンプル）
    eq_weekly = eq_df.resample("W").last().dropna()
    eq_list = [
        {"ts": str(d)[:10], "equity": round(float(e), 2)}
        for d, e in eq_weekly["equity"].items()
    ]

    # equity 2024 のみ（日次）
    eq_2024 = eq_df[(eq_df.index >= "2024-01-01") & (eq_df.index <= "2024-12-31")]
    eq_2024_list = [
        {"ts": str(d)[:10], "equity": round(float(e), 2)}
        for d, e in eq_2024["equity"].items()
    ]

    out = {
        "config_name": "I34 Livermore完全 Lev2.5 ピラミ4",
        "n_trades_total": len(trades_clean),
        "n_liquidations_total": res["n_liquidations"],
        "final": round(res["final"], 2),
        "yearly": yearly,
        "monthly": monthly,
        "trades_2024_sorted_worst_first": trades_2024_sorted[:50],  # 上位50件
        "trades_2024_total": len(trades_2024),
        "reason_2024": reason_2024,
        "sym_2024_list": sym_2024_list,
        "month_2024_list": month_2024_list,
        "btc_prices_2024": btc_prices,
        "equity_weekly": eq_list,
        "equity_2024_daily": eq_2024_list,
    }

    out_path = (Path(__file__).resolve().parent / "results" / "i34_deep_dive.json")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out_path}")
    print(f"   2024 取引数: {len(trades_2024)}")
    print(f"   2024 理由別内訳:")
    for r, v in sorted(reason_2024.items(), key=lambda x: x[1]["pnl"]):
        print(f"      {r:30s}: {v['count']:>4d}件 pnl=${v['pnl']:>+10,.2f} (勝{v['wins']}/負{v['losses']})")
    print(f"\n   2024 ワースト銘柄 Top10:")
    for s in sym_2024_list[:10]:
        print(f"      {s['sym']:15s}: {s['count']:>3d}件 pnl=${s['pnl']:>+10,.2f}")
    print(f"\n   2024 月別:")
    for m in month_2024_list:
        print(f"      {m['month']}: {m['trades']:>3d}件 清算{m['liq']}件 大損失{m['big_losses']}件 pnl=${m['pnl']:>+9,.2f}")


if __name__ == "__main__":
    main()
