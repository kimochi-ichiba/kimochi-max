"""
伝説トレーダー手法 統合バックテストエンジン
===============================================
取り入れる手法:
  - タートル流 Donchian 20/55日 ブレイクアウト
  - リバモア ピラミディング（勝ち銘柄に買い増し）
  - ミネルビニ VCP（ボラ収縮後のブレイクアウト）
  - シュワルツ 短期モメンタム利確
  - シモンズ ミーンリバージョン
  - Seykota トレンド「利益を走らせ損切りを切る」
  - O'Neil CAN SLIM ボリューム急増確認
  - Half-Kelly サイジング
  - 全戦略で清算モデル・日中SL・翌日エントリー
"""
from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125
LIQ_FEE    = 0.002


def liquidation_distance(lev: float) -> float:
    return (1.0 / lev) * 0.85


def _close_pos(state, sym, exit_price, ts, reason, trades, qty_fraction=1.0,
               is_liquidation=False):
    p = state["positions"][sym]
    lev = p["leverage"]
    close_qty = p["qty"] * qty_fraction
    if is_liquidation:
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
    ep = entry_price_open * (1 + SLIP) if direction == "long" else entry_price_open * (1 - SLIP)
    qty = margin / ep
    notional = margin * lev
    state["cash"] -= margin + notional * FEE
    state["positions"][sym] = {
        "side": direction, "qty": qty, "entry_price": ep,
        "leverage": lev, "entry_ts": ts,
        "margin_usd": margin, "peak_price": ep,
        "partial_taken": 0, "pyramids": 0,
    }


def _pyramid_add(state, sym, add_price, add_margin, ts):
    """既存ポジションに買い増し（平均建玉価格を更新）"""
    p = state["positions"][sym]
    lev = p["leverage"]
    ep = add_price * (1 + SLIP) if p["side"] == "long" else add_price * (1 - SLIP)
    add_qty = add_margin / ep
    notional = add_margin * lev
    state["cash"] -= add_margin + notional * FEE
    total_cost = p["entry_price"] * p["qty"] + ep * add_qty
    p["qty"] += add_qty
    p["margin_usd"] += add_margin
    p["entry_price"] = total_cost / p["qty"]
    p["pyramids"] = p.get("pyramids", 0) + 1


def compute_donchian(df, n=20):
    """Donchian channel - N日高値/安値"""
    df = df.copy()
    df[f"dch_h{n}"] = df["high"].rolling(n).max().shift(1)
    df[f"dch_l{n}"] = df["low"].rolling(n).min().shift(1)
    return df


def run_legends(all_data, start, end, cfg, initial=10_000.0):
    """
    伝説トレーダー手法統合バックテスト
    cfg:
      # 基本リスク
      risk_per_trade_pct, max_pos, initial
      # SL / TP / トレーリング
      stop_loss_pct, tp1_pct, tp1_fraction, tp2_pct, tp2_fraction,
      tp3_pct, tp3_fraction (任意), trail_activate_pct, trail_giveback_pct
      # レバレッジ (ADX階層)
      adx_min, adx_lev2, adx_lev3, lev_low, lev_mid, lev_high
      # エントリー条件
      breakout_pct, rsi_long_min, rsi_long_max, rsi_short_min
      # SHORT許可
      enable_short, btc_adx_for_short
      # 利益ロック
      year_profit_lock, profit_lock_pct
      # ★ 伝説手法
      donchian_enabled, donchian_n, donchian_exit_n  # タートル
      pyramid_enabled, pyramid_max, pyramid_trigger_pct, pyramid_size_pct  # リバモア
      vcp_enabled, vcp_atr_max_pct  # ミネルビニ
      volume_confirm, volume_mult  # O'Neil
      target_vol  # ボラターゲット
    """
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)

    # Donchian 前処理
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
    syms = list(all_data.keys())

    state = {"cash": initial, "positions": {}, "locked_bank": 0.0}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    trades = []
    pending_entries = []
    last_year = None
    n_liquidations = 0

    for i, date in enumerate(dates):
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]

        # 年次利益ロック
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

        # 翌日エントリー実行
        if pending_entries:
            for sym, direction, lev, margin, is_pyramid in pending_entries:
                if sym in today_rows:
                    r = today_rows[sym]
                    open_price = r.get("open", r["close"])
                    if is_pyramid:
                        if sym in state["positions"] and margin >= 20 and state["cash"] >= margin * (1 + lev * FEE):
                            _pyramid_add(state, sym, open_price, margin, date)
                    else:
                        if sym not in state["positions"]:
                            if margin < 20 or state["cash"] < margin * (1 + lev * FEE):
                                continue
                            _open_pos(state, sym, open_price, direction, lev, margin, date)
            pending_entries = []

        # 日中SL・清算
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            p = state["positions"][sym]
            high = r.get("high", r["close"])
            low = r.get("low", r["close"])
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
            # SL
            sl_pct = cfg["stop_loss_pct"]
            if p["side"] == "long":
                adverse_worst = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_worst = (high - p["entry_price"]) / p["entry_price"]
            if adverse_worst >= sl_pct:
                sl_price = (p["entry_price"] * (1 - sl_pct) if p["side"] == "long"
                            else p["entry_price"] * (1 + sl_pct))
                _close_pos(state, sym, sl_price, date, "stop_loss_intraday", trades)
                continue

            # Donchian 反対方向ブレイクで決済
            if cfg.get("donchian_enabled"):
                en = cfg.get("donchian_exit_n", 10)
                if p["side"] == "long" and low <= r.get(f"dch_l{en}", -1):
                    _close_pos(state, sym, r.get(f"dch_l{en}", r["close"]), date, "dch_exit", trades)
                    continue
                if p["side"] == "short" and high >= r.get(f"dch_h{en}", 1e18):
                    _close_pos(state, sym, r.get(f"dch_h{en}", r["close"]), date, "dch_exit", trades)
                    continue

            # ピーク・favorable
            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                favorable = (cur - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                favorable = (p["entry_price"] - cur) / p["entry_price"]

            # ピラミディング（リバモア流: 勝ちに追加）
            if cfg.get("pyramid_enabled") and p.get("pyramids", 0) < cfg.get("pyramid_max", 2):
                trigger = cfg.get("pyramid_trigger_pct", 0.10) * (p.get("pyramids", 0) + 1)
                if favorable >= trigger:
                    # 初期ポジション margin × pyramid_size_pct を追加
                    add_margin = p["margin_usd"] * cfg.get("pyramid_size_pct", 0.5) / (p.get("pyramids", 0) + 1)
                    add_margin = min(add_margin, state["cash"] * 0.05)
                    if add_margin >= 20:
                        pending_entries.append((sym, p["side"], p["leverage"], add_margin, True))

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
            # 任意 TP3
            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if cfg.get("tp3_pct") and p_again.get("partial_taken", 0) == 2 and favorable >= cfg["tp3_pct"]:
                _close_pos(state, sym, cur, date, "tp3", trades, qty_fraction=cfg.get("tp3_fraction", 0.5))
                if sym not in state["positions"]: continue

            # トレーリング
            p_again = state["positions"].get(sym)
            if p_again is None: continue
            if p_again["side"] == "long":
                fav_p = (p_again["peak_price"] - p_again["entry_price"]) / p_again["entry_price"]
                gb = (p_again["peak_price"] - cur) / p_again["peak_price"]
            else:
                fav_p = (p_again["entry_price"] - p_again["peak_price"]) / p_again["entry_price"]
                gb = (cur - p_again["peak_price"]) / p_again["peak_price"]
            if fav_p >= cfg["trail_activate_pct"] and gb >= cfg["trail_giveback_pct"]:
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
                    if btc_adx >= cfg.get("btc_adx_for_short", 40) and btc_p < recent_high * 0.97:
                        regime = "bear"

        # レジーム離脱で決済（MR除く: ここでは全員トレンド扱い）
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            p = state["positions"][sym]
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime != "neutral":
                _close_pos(state, sym, today_rows[sym]["close"], date, "regime", trades)

        # エントリー候補抽出
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
                        # Donchian ブレイクアウト確認（タートル流）
                        if cfg.get("donchian_enabled"):
                            n = cfg.get("donchian_n", 20)
                            dch_h = r.get(f"dch_h{n}")
                            dch_l = r.get(f"dch_l{n}")
                            if pd.isna(dch_h) or pd.isna(dch_l): continue
                            if direction == "long" and r["high"] < dch_h: continue
                            if direction == "short" and r["low"] > dch_l: continue
                        # ミネルビニ VCP: ATR縮小
                        if cfg.get("vcp_enabled"):
                            vol_pct = atr / price if price > 0 else 1
                            if vol_pct > cfg.get("vcp_atr_max_pct", 0.06): continue
                        # O'Neil ボリューム急増
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
        "integrity_ok": integrity_gap < 1.5,
        "yearly": yearly, "max_dd": max_dd,
        "n_trades": len(trades),
        "n_liquidations": n_liquidations,
        "n_long": sum(1 for t in trades if t["side"] == "long"),
        "n_short": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": wins / max(len(trades), 1) * 100,
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
        "equity_curve": [{"ts": str(e["ts"])[:10], "equity": round(e["equity"], 2)}
                         for e in equity_curve[::7]],
    }
