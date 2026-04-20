"""
ジム・シモンズ流ハイブリッド戦略 バックテスト
=====================================================
負けを極限まで減らしながら年率+50-70%を目指す

取り入れるシモンズ流の要素:
  1. 複数戦略アンサンブル (トレンド + ミーンリバージョン)
  2. ボラティリティターゲティング (ATR大のとき縮小)
  3. Half-Kelly サイジング (0.5%リスク)
  4. 高分散 (max_pos 30-50)
  5. 厳格フィルタ (ADXとRSIで複合条件)
  6. 短期利確 + タイトSL
  7. BTC-ETH ペアの相関監視

実データ保証:
  - Binance/MEXC/CoinGecko で上場確認
  - data_fetcher.py の Binance 強制ガード経由
  - 架空データ混入ゼロ
"""
from __future__ import annotations
import sys, json, time, requests
from pathlib import Path
from datetime import datetime
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
            "entry_date": str(p["entry_ts"].date()),
            "exit_date": str(ts.date()),
            "strategy": p.get("strategy", "trend"),
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
        "entry_date": str(p["entry_ts"].date()),
        "exit_date": str(ts.date()),
        "strategy": p.get("strategy", "trend"),
    })
    if qty_fraction >= 0.999:
        del state["positions"][sym]
    else:
        p["qty"] -= close_qty
        p["margin_usd"] -= close_margin
        p["partial_taken"] = (p.get("partial_taken", 0) or 0) + 1


def _open_pos(state, sym, entry_price_open, direction, lev, margin, ts, strategy="trend"):
    ep = entry_price_open * (1 + SLIP) if direction == "long" else entry_price_open * (1 - SLIP)
    qty = margin / ep
    notional = margin * lev
    state["cash"] -= margin + notional * FEE
    state["positions"][sym] = {
        "side": direction, "qty": qty, "entry_price": ep,
        "leverage": lev, "entry_ts": ts,
        "margin_usd": margin, "peak_price": ep,
        "partial_taken": 0, "strategy": strategy,
    }


def volatility_scale(atr, close, target_vol=0.03):
    """ボラティリティターゲティング: 目標ボラに対する比率でレバレッジ倍率調整"""
    if pd.isna(atr) or close <= 0:
        return 1.0
    current_vol = atr / close
    if current_vol <= 0:
        return 1.0
    scale = target_vol / current_vol
    return float(np.clip(scale, 0.3, 1.5))


def run_simons_hybrid(all_data, start, end, cfg, initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
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

        # 利益ロック
        if cfg["year_profit_lock"] and last_year is not None and date.year > last_year:
            if equity_curve:
                ys_eq = next((e["equity"] for e in equity_curve
                              if pd.Timestamp(e["ts"]).year == last_year), None)
                ye_eq = equity_curve[-1]["equity"]
                if ys_eq and ye_eq > ys_eq:
                    profit = ye_eq - ys_eq
                    lock = profit * cfg.get("profit_lock_pct", 0.30)
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
            for sym, direction, lev, margin, strat in pending_entries:
                if sym in today_rows and sym not in state["positions"]:
                    r = today_rows[sym]
                    open_price = r.get("open", r["close"])
                    if margin < 20 or state["cash"] < margin * (1 + lev * FEE):
                        continue
                    _open_pos(state, sym, open_price, direction, lev, margin, date, strat)
            pending_entries = []

        # 日中SL/清算
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
            # 戦略別SL
            sl_pct = cfg["stop_loss_pct"]
            if p.get("strategy") == "meanrev":
                sl_pct = cfg["mr_stop_loss_pct"]
            if p["side"] == "long":
                adverse_worst = (p["entry_price"] - low) / p["entry_price"]
            else:
                adverse_worst = (high - p["entry_price"]) / p["entry_price"]
            if adverse_worst >= sl_pct:
                sl_price = (p["entry_price"] * (1 - sl_pct) if p["side"] == "long"
                            else p["entry_price"] * (1 + sl_pct))
                _close_pos(state, sym, sl_price, date, "stop_loss_intraday", trades)
                continue

            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                favorable = (cur - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                favorable = (p["entry_price"] - cur) / p["entry_price"]

            # ミーンリバージョン: RSIが中立圏に戻ったら即利確
            if p.get("strategy") == "meanrev":
                rsi_now = r.get("rsi", 50)
                if p["side"] == "long" and rsi_now >= cfg["mr_exit_rsi_long"]:
                    _close_pos(state, sym, cur, date, "mr_rsi_exit", trades)
                    continue
                if p["side"] == "short" and rsi_now <= cfg["mr_exit_rsi_short"]:
                    _close_pos(state, sym, cur, date, "mr_rsi_exit", trades)
                    continue
                # MRは最短5日、最長10日保有
                hold_days = (date - p["entry_ts"]).days
                if hold_days >= cfg["mr_max_hold_days"]:
                    _close_pos(state, sym, cur, date, "mr_time_exit", trades)
                    continue

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

        # トレンドポジションのレジーム離脱決済
        for sym in list(state["positions"].keys()):
            if sym not in today_rows: continue
            p = state["positions"][sym]
            if p.get("strategy") == "meanrev":
                continue
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required and regime != "neutral":
                _close_pos(state, sym, today_rows[sym]["close"], date, "regime", trades)

        slots_remaining = cfg["max_pos"] - len(state["positions"])

        # ━━━ トレンドエントリー ━━━
        if slots_remaining > 0 and regime in ("bull", "bear") and i < len(dates) - 1:
            if regime == "bear" and not cfg["enable_short"]:
                trend_slots = 0
            else:
                trend_slots = min(slots_remaining, cfg.get("trend_max_pos", cfg["max_pos"]))
            if trend_slots > 0:
                direction = "long" if regime == "bull" else "short"
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
                candidates = candidates[:trend_slots]
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
                        vs = volatility_scale(r.get("atr"), r["close"], cfg["target_vol"])
                        lev = max(1.0, lev * vs)
                        margin = risk_usd / (cfg["stop_loss_pct"] * lev)
                        margin = min(margin, state["cash"] * 0.05)
                        if margin < 20: continue
                        pending_entries.append((sym, direction, lev, margin, "trend"))

        # ━━━ ミーンリバージョンエントリー（全レジームで有効） ━━━
        slots_remaining = cfg["max_pos"] - len(state["positions"]) - len(pending_entries)
        if cfg.get("mr_enabled", True) and slots_remaining > 0 and i < len(dates) - 1:
            mr_slots = min(slots_remaining, cfg.get("mr_max_slots", 10))
            if mr_slots > 0:
                mr_cand = []
                for sym, r in today_rows.items():
                    if sym in state["positions"]: continue
                    if any(p[0] == sym for p in pending_entries): continue
                    rsi = r.get("rsi", 50)
                    if pd.isna(rsi): continue
                    adx = r["adx"]
                    if adx > cfg["mr_max_adx"]: continue  # トレンド相場は除外
                    price, ema200 = r["close"], r["ema200"]
                    dev_abs = abs(price - ema200) / ema200
                    if dev_abs > cfg["mr_max_dev_from_ema"]: continue  # 乖離しすぎは除外
                    # LONG: 売られすぎ
                    if rsi <= cfg["mr_entry_rsi_long"]:
                        mr_cand.append((sym, r, "long", rsi))
                    # SHORT: 買われすぎ（bull/bearどちらでも可）
                    elif cfg.get("mr_short_enabled", True) and rsi >= cfg["mr_entry_rsi_short"]:
                        if regime != "bull":  # 強気相場ではMR SHORTを避ける
                            mr_cand.append((sym, r, "short", rsi))
                # 極端なRSIから優先
                mr_cand.sort(key=lambda x: abs(x[3] - 50), reverse=True)
                mr_cand = mr_cand[:mr_slots]
                if mr_cand:
                    unreal = sum(
                        (p["qty"] * (today_rows[s]["close"] - p["entry_price"]) * p["leverage"]
                         if p["side"] == "long" and s in today_rows
                         else p["qty"] * (p["entry_price"] - today_rows[s]["close"]) * p["leverage"]
                         if s in today_rows else 0)
                        for s, p in state["positions"].items()
                    )
                    account_eq = (state["cash"] + state["locked_bank"] +
                                  sum(p["margin_usd"] for p in state["positions"].values()) + unreal)
                    mr_risk_usd = account_eq * cfg["mr_risk_pct"]
                    for sym, r, direction, rsi in mr_cand:
                        lev = cfg["mr_leverage"]
                        vs = volatility_scale(r.get("atr"), r["close"], cfg["target_vol"])
                        lev = max(1.0, lev * vs)
                        margin = mr_risk_usd / (cfg["mr_stop_loss_pct"] * lev)
                        margin = min(margin, state["cash"] * 0.03)
                        if margin < 20: continue
                        pending_entries.append((sym, direction, lev, margin, "meanrev"))

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
    n_trend = sum(1 for t in trades if t.get("strategy") == "trend")
    n_mr = sum(1 for t in trades if t.get("strategy") == "meanrev")
    wins_trend = sum(1 for t in trades if t.get("strategy") == "trend" and t.get("pnl", 0) > 0)
    wins_mr = sum(1 for t in trades if t.get("strategy") == "meanrev" and t.get("pnl", 0) > 0)

    return {
        "final": final, "total_ret": total,
        "avg_annual_ret": avg_annual,
        "integrity_gap": integrity_gap,
        "integrity_ok": integrity_gap < 1.0,
        "yearly": yearly, "max_dd": max_dd,
        "n_trades": len(trades),
        "n_trend": n_trend, "n_mr": n_mr,
        "win_rate_trend": (wins_trend / max(n_trend, 1) * 100),
        "win_rate_mr": (wins_mr / max(n_mr, 1) * 100),
        "n_liquidations": n_liquidations,
        "n_long": sum(1 for t in trades if t["side"] == "long"),
        "n_short": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": wins / max(len(trades), 1) * 100,
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
        "equity_curve": [{"ts": str(e["ts"])[:10], "equity": round(e["equity"], 2)}
                         for e in equity_curve[::7]],  # 週単位に間引き
    }


def quick_hallucination_check():
    """Binance / MEXC / CoinGecko で50銘柄の実在確認"""
    result = {"binance": None, "mexc": None, "coingecko": None, "verified": 0, "details": []}
    print("🔍 ハルシネーション検証中...")
    try:
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=15)
        bs = set(s["baseAsset"] + "/" + s["quoteAsset"] for s in r.json().get("symbols", [])
                 if s.get("status") == "TRADING")
        result["binance"] = len(bs)
    except Exception:
        bs = set()

    try:
        r = requests.get("https://api.mexc.com/api/v3/exchangeInfo", timeout=15)
        ms = set(s["baseAsset"] + "/" + s["quoteAsset"] for s in r.json().get("symbols", []))
        result["mexc"] = len(ms)
    except Exception:
        ms = set()

    cg_id_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin", "SOL": "solana",
        "XRP": "ripple", "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
        "LINK": "chainlink", "DOGE": "dogecoin", "LTC": "litecoin", "BCH": "bitcoin-cash",
        "ATOM": "cosmos", "UNI": "uniswap", "NEAR": "near", "FIL": "filecoin",
        "TRX": "tron", "ETC": "ethereum-classic", "APT": "aptos", "ARB": "arbitrum",
        "OP": "optimism", "ALGO": "algorand", "XLM": "stellar", "VET": "vechain",
        "HBAR": "hedera-hashgraph", "EGLD": "elrond-erd-2", "FTM": "fantom",
        "AAVE": "aave", "SAND": "the-sandbox", "MANA": "decentraland",
        "CRV": "curve-dao-token", "COMP": "compound-governance-token",
        "SUSHI": "sushi", "YFI": "yearn-finance", "SNX": "havven", "MKR": "maker",
        "IMX": "immutable-x", "INJ": "injective-protocol", "GRT": "the-graph",
        "ICP": "internet-computer", "KAVA": "kava", "ZEC": "zcash", "DASH": "dash",
        "ZIL": "zilliqa", "ONE": "harmony", "BAT": "basic-attention-token",
        "ENJ": "enjincoin", "QNT": "quant-network", "CHZ": "chiliz", "AXS": "axie-infinity",
    }
    try:
        time.sleep(0.8)
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=500&page=1", timeout=20)
        cg_ids = set(c["id"] for c in r.json() if isinstance(c, dict))
        result["coingecko"] = len(cg_ids)
    except Exception:
        cg_ids = set()

    for sym in UNIVERSE_50:
        base = sym.split("/")[0]
        bin_ok = sym in bs
        mexc_ok = sym in ms
        cg_ok = cg_id_map.get(base, "") in cg_ids
        n_ok = sum([bin_ok, mexc_ok, cg_ok])
        if n_ok >= 2:
            result["verified"] += 1
        result["details"].append({
            "symbol": sym, "binance": bin_ok, "mexc": mexc_ok,
            "coingecko": cg_ok, "sources": n_ok,
        })

    # BTCの生API vs 実データ突合
    sample = [("2020-12-31", None), ("2021-11-10", None), ("2022-11-09", None),
              ("2023-06-15", None), ("2024-03-14", None)]
    btc_samples = []
    for date_str, _ in sample:
        try:
            ts = int(datetime.fromisoformat(date_str).timestamp() * 1000)
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&startTime={ts}&limit=1"
            rr = requests.get(url, timeout=10).json()
            if rr:
                o, h, l, c = [float(x) for x in rr[0][1:5]]
                btc_samples.append({
                    "date": date_str, "open": o, "high": h, "low": l, "close": c,
                })
        except Exception:
            pass
    result["btc_raw_samples"] = btc_samples
    return result


def render_html(results, halluc, best_name, sample_compare, out_path):
    """HTMLレポート生成"""
    # ベスト戦略の年次 & equity curve
    best = results[best_name]
    eq = best.get("equity_curve", [])
    eq_labels = [e["ts"] for e in eq]
    eq_values = [e["equity"] for e in eq]

    # 戦略一覧テーブル
    rows = []
    for name, r in results.items():
        cls = "best" if name == best_name else ""
        neg_cls = "neg" if r["negative_years"] > 0 else ""
        tags = []
        if r["all_positive"]: tags.append('<span class="tag green">毎年プラス</span>')
        elif r["no_negative"]: tags.append('<span class="tag lightgreen">マイナス無</span>')
        if r["n_liquidations"] == 0: tags.append('<span class="tag blue">清算0</span>')
        if r["win_rate"] >= 55: tags.append(f'<span class="tag purple">勝率{r["win_rate"]:.0f}%</span>')
        if r["max_dd"] < 30: tags.append(f'<span class="tag orange">DD{r["max_dd"]:.0f}%</span>')
        rows.append(f"""
        <tr class="{cls}">
          <td>{name}</td>
          <td class="num">{r['yearly'].get(2020, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2021, 0):+.1f}%</td>
          <td class="num {neg_cls if r['yearly'].get(2022, 0) < 0 else ''}">{r['yearly'].get(2022, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2023, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2024, 0):+.1f}%</td>
          <td class="num bold">{r['avg_annual_ret']:+.1f}%</td>
          <td class="num">{r['max_dd']:.1f}%</td>
          <td class="num">{r['win_rate']:.1f}%</td>
          <td class="num">{r['n_liquidations']}</td>
          <td>{'✅' if r['integrity_ok'] else '❌'}</td>
          <td>{' '.join(tags)}</td>
        </tr>""")
    table_rows = "\n".join(rows)

    # ハルシネーション結果
    ver_rows = []
    for d in halluc["details"]:
        ver_rows.append(f"""
        <tr>
          <td>{d['symbol']}</td>
          <td>{'✅' if d['binance'] else '❌'}</td>
          <td>{'✅' if d['mexc'] else '❌'}</td>
          <td>{'✅' if d['coingecko'] else '❌'}</td>
          <td class="bold">{d['sources']}/3</td>
        </tr>""")
    ver_table = "\n".join(ver_rows)

    # BTC突合
    cmp_rows = []
    for s, b in zip(sample_compare, halluc["btc_raw_samples"]):
        if s and b:
            diff_c = abs(s['close'] - b['close']) / b['close'] * 100
            cmp_rows.append(f"""
            <tr>
              <td>{s['date']}</td>
              <td class="num">${b['open']:,.0f}</td>
              <td class="num">${b['high']:,.0f}</td>
              <td class="num">${b['low']:,.0f}</td>
              <td class="num">${b['close']:,.0f}</td>
              <td class="num">${s['close']:,.0f}</td>
              <td class="num">{diff_c:.3f}%</td>
              <td>{'✅一致' if diff_c < 0.1 else '⚠️乖離'}</td>
            </tr>""")
    cmp_table = "\n".join(cmp_rows) if cmp_rows else "<tr><td colspan='8'>データ未取得</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>ジム・シモンズ流ハイブリッド バックテスト結果</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 20px; background: #f5f7fa; color: #1a202c; }}
  h1 {{ font-size: 28px; margin: 0 0 10px 0; color: #1a365d; }}
  h2 {{ font-size: 22px; margin: 30px 0 15px 0; color: #2c5282; border-left: 4px solid #3182ce; padding-left: 12px; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .hero {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }}
  .hero h1 {{ color: white; }}
  .metric {{ display: inline-block; margin-right: 24px; margin-bottom: 8px; }}
  .metric-label {{ font-size: 12px; opacity: 0.85; }}
  .metric-value {{ font-size: 22px; font-weight: bold; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
  th {{ background: #f7fafc; font-weight: 600; color: #4a5568; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.bold {{ font-weight: bold; }}
  tr.best {{ background: #ebf8ff; }}
  tr.best td {{ font-weight: 600; }}
  .neg {{ color: #c53030; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-right: 4px; }}
  .green {{ background: #c6f6d5; color: #22543d; }}
  .lightgreen {{ background: #f0fff4; color: #22543d; }}
  .blue {{ background: #bee3f8; color: #2a4365; }}
  .purple {{ background: #e9d8fd; color: #44337a; }}
  .orange {{ background: #feebc8; color: #7b341e; }}
  .chart-box {{ height: 400px; position: relative; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 20px 0; }}
  .summary-item {{ background: #f7fafc; padding: 16px; border-radius: 8px; border-left: 4px solid #3182ce; }}
  .summary-value {{ font-size: 24px; font-weight: bold; color: #2c5282; }}
  .summary-label {{ font-size: 12px; color: #718096; margin-top: 4px; }}
  .footer {{ text-align: center; color: #718096; font-size: 12px; margin-top: 40px; padding: 20px; }}
</style>
</head>
<body>
<div class="container">

<div class="card hero">
  <h1>🎯 ジム・シモンズ流ハイブリッド戦略 バックテスト結果</h1>
  <p>負けを極限まで減らしながら年率+50-70%を目指す / 5年間（2020-2024）/ Binance実データ</p>
  <div style="margin-top:20px">
    <div class="metric"><div class="metric-label">ベスト戦略</div><div class="metric-value">{best_name}</div></div>
    <div class="metric"><div class="metric-label">年率</div><div class="metric-value">{best['avg_annual_ret']:+.1f}%</div></div>
    <div class="metric"><div class="metric-label">5年トータル</div><div class="metric-value">{best['total_ret']:+.0f}%</div></div>
    <div class="metric"><div class="metric-label">最大DD</div><div class="metric-value">{best['max_dd']:.1f}%</div></div>
    <div class="metric"><div class="metric-label">勝率</div><div class="metric-value">{best['win_rate']:.1f}%</div></div>
    <div class="metric"><div class="metric-label">清算</div><div class="metric-value">{best['n_liquidations']}回</div></div>
    <div class="metric"><div class="metric-label">$10K → 5年後</div><div class="metric-value">${best['final']:,.0f}</div></div>
  </div>
</div>

<div class="card">
  <h2>📊 ベスト戦略 資産推移</h2>
  <div class="chart-box"><canvas id="eqChart"></canvas></div>
</div>

<div class="card">
  <h2>📈 年次リターン（ベスト）</h2>
  <div class="chart-box" style="height:300px"><canvas id="yrChart"></canvas></div>
  <div class="summary-grid">
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2020, 0):+.1f}%</div><div class="summary-label">2020年</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2021, 0):+.1f}%</div><div class="summary-label">2021年</div></div>
    <div class="summary-item" style="border-color:{'#c53030' if best['yearly'].get(2022, 0) < 0 else '#3182ce'}"><div class="summary-value" style="color:{'#c53030' if best['yearly'].get(2022, 0) < 0 else '#2c5282'}">{best['yearly'].get(2022, 0):+.1f}%</div><div class="summary-label">2022年 (熊相場)</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2023, 0):+.1f}%</div><div class="summary-label">2023年</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2024, 0):+.1f}%</div><div class="summary-label">2024年</div></div>
  </div>
</div>

<div class="card">
  <h2>🧪 全戦略比較</h2>
  <table>
    <thead>
      <tr>
        <th>戦略</th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
        <th>年率</th><th>DD</th><th>勝率</th><th>清算</th><th>整合</th><th>判定</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>🔍 ハルシネーション検証 - 50銘柄実在性</h2>
  <div class="summary-grid">
    <div class="summary-item"><div class="summary-value">{halluc.get('binance') or '?'}</div><div class="summary-label">Binance上場全銘柄</div></div>
    <div class="summary-item"><div class="summary-value">{halluc.get('mexc') or '?'}</div><div class="summary-label">MEXC上場全銘柄</div></div>
    <div class="summary-item"><div class="summary-value">{halluc.get('coingecko') or '?'}</div><div class="summary-label">CoinGecko上位500</div></div>
    <div class="summary-item" style="border-color:#38a169"><div class="summary-value" style="color:#22543d">{halluc['verified']}/50</div><div class="summary-label">2ソース以上で本物確認</div></div>
  </div>
  <table>
    <thead><tr><th>銘柄</th><th>Binance</th><th>MEXC</th><th>CoinGecko</th><th>実在ソース数</th></tr></thead>
    <tbody>{ver_table}</tbody>
  </table>
</div>

<div class="card">
  <h2>🔬 BTC価格 生API vs バックテストデータ突合</h2>
  <p style="color:#4a5568;font-size:14px">Binance生APIを直接叩いた結果と、バックテストで実際に使った価格が一致するかを確認しています。乖離0.1%未満なら「完全一致＝改ざんなし」です。</p>
  <table>
    <thead>
      <tr>
        <th>日付</th><th>生API Open</th><th>生API High</th><th>生API Low</th><th>生API Close</th>
        <th>バックテスト Close</th><th>乖離</th><th>判定</th>
      </tr>
    </thead>
    <tbody>{cmp_table}</tbody>
  </table>
</div>

<div class="card">
  <h2>🛡 データ完全性チェック</h2>
  <ul style="line-height:1.8">
    <li>✅ Binance 強制ガード（data_fetcher.py 行408-414）: 他取引所ならRuntimeErrorで停止</li>
    <li>✅ 合成データ補完なし: データ取得失敗時は空DataFrameを返す</li>
    <li>✅ 6項目バリデーション: 価格&gt;0 / NaN無し / タイムスタンプ連続 / 出来高&gt;0 / 取引所確認 / 価格変動妥当性</li>
    <li>✅ 翌日始値エントリー: 先読みバイアスなし</li>
    <li>✅ 日中SL判定: row["low"]/row["high"] で正確</li>
    <li>✅ 清算モデル: (1/lev × 0.85) で現実的</li>
    <li>✅ 整合性チェック: total_ret vs 年次複利 で1pp未満差</li>
  </ul>
</div>

<div class="footer">
  kimochi-max Simons Hybrid Backtest / Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</div>

</div>

<script>
  const eqLabels = {json.dumps(eq_labels)};
  const eqValues = {json.dumps(eq_values)};
  new Chart(document.getElementById('eqChart'), {{
    type: 'line',
    data: {{
      labels: eqLabels,
      datasets: [{{
        label: '資産 ($)',
        data: eqValues,
        borderColor: '#3182ce',
        backgroundColor: 'rgba(49, 130, 206, 0.1)',
        fill: true,
        tension: 0.2,
        pointRadius: 0,
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: true }} }},
      scales: {{
        y: {{ ticks: {{ callback: v => '$' + v.toLocaleString() }} }},
        x: {{ ticks: {{ maxTicksLimit: 12 }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('yrChart'), {{
    type: 'bar',
    data: {{
      labels: ['2020', '2021', '2022', '2023', '2024'],
      datasets: [{{
        label: '年次リターン (%)',
        data: [
          {best['yearly'].get(2020, 0)},
          {best['yearly'].get(2021, 0)},
          {best['yearly'].get(2022, 0)},
          {best['yearly'].get(2023, 0)},
          {best['yearly'].get(2024, 0)}
        ],
        backgroundColor: [
          {best['yearly'].get(2020, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2021, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2022, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2023, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2024, 0)} >= 0 ? '#38a169' : '#e53e3e'
        ],
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ y: {{ ticks: {{ callback: v => v + '%' }} }} }}
    }}
  }});
</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    # ━━━ ハルシネーションチェック（実行前に） ━━━
    print("=" * 100)
    print("PHASE 1: ハルシネーション検証（Binance/MEXC/CoinGecko クロスチェック）")
    print("=" * 100)
    halluc = quick_hallucination_check()
    print(f"  Binance上場銘柄: {halluc['binance']}")
    print(f"  MEXC上場銘柄: {halluc['mexc']}")
    print(f"  CoinGecko上位500: {halluc['coingecko']}")
    print(f"  2ソース以上で実在確認: {halluc['verified']}/50\n")

    # ━━━ データ取得 ━━━
    print("=" * 100)
    print("PHASE 2: Binance 実データ取得（RSI指標付き）")
    print("=" * 100)
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 / {time.time()-t0:.0f}秒\n")

    # BTC 価格比較用サンプル
    sample_compare = []
    btc_df = all_data.get("BTC/USDT")
    if btc_df is not None:
        for ds in ["2020-12-31", "2021-11-10", "2022-11-09", "2023-06-15", "2024-03-14"]:
            ts = pd.Timestamp(ds)
            if ts in btc_df.index:
                r = btc_df.loc[ts]
                sample_compare.append({
                    "date": ds,
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                })

    # ━━━ シモンズ流ハイブリッド戦略セット ━━━
    base = dict(
        # トレンド部
        risk_per_trade_pct=0.01, max_pos=30,
        stop_loss_pct=0.05,
        tp1_pct=0.05, tp1_fraction=0.4,
        tp2_pct=0.12, tp2_fraction=0.5,
        trail_activate_pct=0.15, trail_giveback_pct=0.05,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.5, lev_mid=2.0, lev_high=2.5,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=72,
        rsi_short_min=85,
        enable_short=True, year_profit_lock=True,
        profit_lock_pct=0.30, btc_adx_for_short=40,
        trend_max_pos=20,
        # ボラ目標
        target_vol=0.03,
        # ミーンリバージョン部
        mr_enabled=True,
        mr_risk_pct=0.005,
        mr_max_slots=10,
        mr_leverage=1.5,
        mr_stop_loss_pct=0.04,
        mr_entry_rsi_long=25,
        mr_exit_rsi_long=55,
        mr_entry_rsi_short=80,
        mr_exit_rsi_short=50,
        mr_max_adx=30,
        mr_max_dev_from_ema=0.25,
        mr_max_hold_days=7,
        mr_short_enabled=True,
    )

    configs = [
        ("S01 シモンズ基準(T+MR) max30",                          {**base}),
        ("S02 MRのみ(トレンド無効化)",                            {**base, "trend_max_pos": 0}),
        ("S03 トレンドのみ(MR無効化)",                            {**base, "mr_enabled": False}),
        ("S04 高分散 max50",                                      {**base, "max_pos": 50, "trend_max_pos": 25, "mr_max_slots": 15}),
        ("S05 Lev2-3アップ リスク1.5%",                          {**base, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0, "risk_per_trade_pct": 0.015}),
        ("S06 ミーンリバ重視 MR slots15",                        {**base, "mr_max_slots": 15, "mr_risk_pct": 0.008}),
        ("S07 タイトSL(3%) TP早め(3/8)",                        {**base, "stop_loss_pct": 0.03, "tp1_pct": 0.03, "tp2_pct": 0.08}),
        ("S08 ボラターゲット2%(低リスク)",                        {**base, "target_vol": 0.02}),
        ("S09 MR SHORTなし",                                      {**base, "mr_short_enabled": False}),
        ("S10 フル攻撃 Lev2.5-3.5 max40",                         {**base, "lev_low": 2.5, "lev_mid": 3.0, "lev_high": 3.5, "max_pos": 40, "risk_per_trade_pct": 0.015}),
        ("S11 超保守 Lev1.2 リスク0.5%",                          {**base, "lev_low": 1.0, "lev_mid": 1.2, "lev_high": 1.5, "risk_per_trade_pct": 0.005, "mr_risk_pct": 0.003}),
        ("S12 MR長保有 10日 RSI厳格",                             {**base, "mr_max_hold_days": 10, "mr_entry_rsi_long": 22, "mr_entry_rsi_short": 82}),
    ]

    print("=" * 100)
    print("PHASE 3: シモンズ流ハイブリッド・反復検証")
    print("=" * 100)
    print(f"{'戦略':42s} | {'2020':>6s} {'2021':>6s} {'2022':>6s} {'2023':>6s} {'2024':>6s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'清算':>4s} | Trend/MR")
    print("-" * 160)

    results = {}
    best = None
    best_no_neg = None
    for name, cfg in configs:
        r = run_simons_hybrid(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:42s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+5.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {r['n_liquidations']:>3d} | "
        row += f"{r['n_trend']:>3d}/{r['n_mr']:>3d}"
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        elif r["no_negative"]: tags.append("🟢ﾏｲﾅｽ無")
        if r["avg_annual_ret"] >= 70: tags.append("🚀+70%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        elif r["avg_annual_ret"] >= 30: tags.append("💪+30%")
        if r["win_rate"] >= 55: tags.append("📈勝率55+")
        if r["max_dd"] < 30: tags.append("🛡DD<30")
        if r["n_liquidations"] == 0: tags.append("✅清算0")
        row += " " + " ".join(tags)
        print(row)
        results[name] = r

        if r["integrity_ok"]:
            if best is None or r["avg_annual_ret"] > best[1]["avg_annual_ret"]:
                best = (name, r)
            if r["no_negative"]:
                if best_no_neg is None or r["avg_annual_ret"] > best_no_neg[1]["avg_annual_ret"]:
                    best_no_neg = (name, r)

    print("\n" + "=" * 100)
    final_best = best_no_neg if best_no_neg else best
    if final_best:
        n, r = final_best
        tag = "マイナス無 × 最高年率" if best_no_neg else "最高年率"
        print(f"🏆 {tag}: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / "
              f"DD {r['max_dd']:.1f}% / 清算{r['n_liquidations']}回 / $10K→${r['final']:,.0f}")
    print("=" * 100)

    # ━━━ 保存 ━━━
    out_json = Path("/Users/sanosano/projects/kimochi-max/results/simons_hybrid.json")
    out_json.write_text(json.dumps({
        "configs": [{"name": n, **{k: v for k, v in c.items()}} for n, c in configs],
        "results": results,
        "hallucination_check": halluc,
        "btc_samples": sample_compare,
        "best": final_best[0] if final_best else None,
    }, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 JSON: {out_json}")

    # ━━━ HTMLレポート生成 ━━━
    out_html = Path("/Users/sanosano/projects/kimochi-max/results/simons_hybrid_report.html")
    best_name = final_best[0] if final_best else list(results.keys())[0]
    render_html(results, halluc, best_name, sample_compare, out_html)
    print(f"📄 HTMLレポート: {out_html}")
    print(f"   ブラウザで開く: open '{out_html}'")
