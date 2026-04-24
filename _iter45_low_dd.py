"""
Iter45: 低DD特化 (精神的ラク) 戦略バックテスト
===================================================
目的:
  DD 50〜70%を 30%以下に抑えながら、できる限りリターンを確保する。

10パターン:
  D01 BTC/ETH/USDT 3分割 (33/33/34) 月次リバランス, USDT年3%
  D02 BTC/USDT 50/50 月次リバランス (超保守)
  D03 BTC/ETH/USDT 25/25/50 (現金厚め)
  D04 BTCマイルド（EMA100で厳し目） + USDT金利3%
  D05 モメンタムTop3 + DD上限ルール（月次-10%で一時停止）
  D06 モメンタムTop3 25% + BTCマイルド 25% + USDT 50%
  D07 BTCマイルド + モメンタムTop3 両方 60/40 + 共通DD上限
  D08 週次BTCリバランス (EMA50上で保有, 以下で撤退)
  D09 「静」ハイブリッド: BTCマイルド70% + モメンタム30% (保守寄り)
  D10 究極分散: BTC20%+ETH20%+モメンタム20%+USDT40%
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
import _iter43_rethink as R43

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter45_low_dd.json")

FEE = 0.0006
SLIP = 0.0003


def load_data():
    with open(CACHE_PATH, "rb") as f:
        return pickle.load(f)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D01, D02, D03: 固定比率分散 (月次リバランス、USDT金利3%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_diversified(all_data, start, end, weights, cash_rate=0.03, initial=10_000.0):
    """
    weights: {"BTC/USDT": 0.33, "ETH/USDT": 0.33, "USDT": 0.34}
    月次リバランス。USDT部分は年3%金利で複利。
    """
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    crypto_syms = [s for s in weights.keys() if s != "USDT"]
    usdt_weight = weights.get("USDT", 0)

    # 初期購入
    qtys = {}
    usdt_cash = 0.0
    first_date = dates[0]
    for sym in crypto_syms:
        w = weights[sym]
        df = all_data[sym]
        if first_date not in df.index:
            first_date = df.index[df.index >= start_ts][0]
        price = df.loc[first_date, "close"] * (1 + SLIP)
        qtys[sym] = (initial * w) / price * (1 - FEE)
    usdt_cash = initial * usdt_weight

    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    last_month = dates[0].month
    prev_date = dates[0]

    for date in dates:
        # USDT金利 (日割複利)
        if usdt_cash > 0 and prev_date is not None:
            days = (date - prev_date).days
            usdt_cash *= (1 + cash_rate) ** (days / 365)
        prev_date = date

        # 月次リバランス
        if date.month != last_month:
            # 現時価評価
            total = usdt_cash
            for sym in crypto_syms:
                df = all_data[sym]
                if date in df.index:
                    total += qtys[sym] * df.loc[date, "close"]
            # ターゲット配分に再配分
            new_qtys = {}
            for sym in crypto_syms:
                df = all_data[sym]
                if date not in df.index: continue
                target = total * weights[sym]
                price = df.loc[date, "close"]
                new_qtys[sym] = target / price * (1 - FEE)
            qtys = new_qtys
            usdt_cash = total * usdt_weight
            last_month = date.month

        # 時価評価
        total = usdt_cash
        for sym in crypto_syms:
            df = all_data[sym]
            if date in df.index:
                total += qtys[sym] * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=len(crypto_syms) * 60)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D04: BTCマイルド厳し目（EMA100で撤退）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_btc_strict(all_data, start, end, cash_rate=0.03, ema_col="ema50", initial=10_000.0):
    """EMA50の上で保有、下で現金 (EMA200より反応早い)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial; qty = 0.0; position = False
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0; prev_date = None

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]
        ema = r.get(ema_col)
        if not position and prev_date is not None:
            days = (date - prev_date).days
            cash *= (1 + cash_rate) ** (days / 365)
        prev_date = date

        if pd.isna(ema):
            equity_curve.append({"ts": date, "equity": cash + qty * price})
            continue

        if price > ema and not position:
            buy_price = price * (1 + SLIP)
            qty = cash / buy_price * (1 - FEE)
            cash = 0; position = True; n_trades += 1
        elif price < ema and position:
            sell_price = price * (1 - SLIP)
            cash = qty * sell_price * (1 - FEE)
            qty = 0; position = False; n_trades += 1

        equity_curve.append({"ts": date, "equity": cash + qty * price})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D05: モメンタムTop3 + DD上限
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_momentum_dd_capped(all_data, start, end, top_n=3, lookback_days=90,
                            dd_trigger_pct=10.0, cooldown_days=30,
                            initial=10_000.0):
    """
    モメンタム戦略 + DD上限ルール:
      - 資産が前回ピークから -dd_trigger_pct% 下落したら、cooldown_days間は新規停止
      - cooldown中は既存ポジションも決済して全額現金化
    """
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    last_rebalance_month = None
    peak_equity = initial
    cooldown_until = None

    for date in dates:
        # 時価評価 + ピーク・DD判定
        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        peak_equity = max(peak_equity, total)
        cur_dd = (peak_equity - total) / peak_equity * 100 if peak_equity > 0 else 0

        # DD上限判定
        if cur_dd >= dd_trigger_pct and cooldown_until is None:
            # 全決済
            for sym, qty in list(positions.items()):
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - SLIP)
                    cash += qty * price * (1 - FEE)
            positions.clear()
            cooldown_until = date + pd.Timedelta(days=cooldown_days)
            n_trades += 1

        # cooldown解除
        if cooldown_until is not None and date >= cooldown_until:
            cooldown_until = None
            peak_equity = cash  # cooldown解除時点でピーク更新

        # 月次リバランス
        rebalance = (last_rebalance_month is None) or (date.month != last_rebalance_month)
        if rebalance and cooldown_until is None:
            # 現時価確定
            total = cash
            for sym, qty in positions.items():
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - SLIP)
                    total += qty * price * (1 - FEE)
            positions.clear()
            cash = total

            # BTC regime
            btc_r = btc_df.loc[date]
            btc_price = btc_r["close"]; btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_price < btc_ema200:
                # 現金
                last_rebalance_month = date.month
            else:
                # Top N 選択
                scores = []
                for sym, df in all_data.items():
                    if date not in df.index: continue
                    past_idx = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
                    if len(past_idx) < 20: continue
                    ret = df.loc[date, "close"] / df.loc[past_idx[0], "close"] - 1
                    adx = df.loc[date].get("adx", 0)
                    if pd.isna(adx) or adx < 20: continue
                    scores.append((sym, ret))
                scores.sort(key=lambda x: x[1], reverse=True)
                selected = scores[:top_n]
                if selected:
                    w = 1.0 / len(selected)
                    for sym, _ in selected:
                        df = all_data[sym]
                        price = df.loc[date, "close"] * (1 + SLIP)
                        qty = cash * w / price * (1 - FEE)
                        positions[sym] = qty
                        n_trades += 1
                    cash = 0
                last_rebalance_month = date.month

        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D06-D10: ポートフォリオ合成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_multi_portfolio(all_data, start, end, parts, initial=10_000.0):
    """
    parts = [
        {"fn": run_momentum, "kwargs": {...}, "weight": 0.25},
        {"fn": run_btc_mild, "kwargs": {...}, "weight": 0.25},
        {"weight": 0.50, "usdt_rate": 0.03},  # USDT保有部分
    ]
    各パートを独立にバックテストして、週次equityを重み付き合算する。
    """
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    # 各パート実行
    part_eqs = []
    total_trades = 0
    for p in parts:
        part_init = initial * p["weight"]
        if p.get("fn") is None:
            # USDT金利
            rate = p.get("usdt_rate", 0.03)
            eq_list = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": part_init}]
            cash = part_init
            prev = dates[0]
            for d in dates:
                days = (d - prev).days
                cash *= (1 + rate) ** (days / 365)
                prev = d
                eq_list.append({"ts": d, "equity": cash})
            part_eqs.append(eq_list)
        else:
            r = p["fn"](all_data, start, end, initial=part_init, **p.get("kwargs", {}))
            # 週次 equity を日次に補間
            eq_weekly = r["equity_weekly"]
            # 日次は持っていないので週次をそのまま使う
            part_eqs.append([{"ts": pd.Timestamp(e["ts"]), "equity": e["equity"]} for e in eq_weekly])
            total_trades += r.get("n_trades", 0)

    # 合算 (日付ベース)
    all_ts = sorted(set(ts for part in part_eqs for ts in (e["ts"] for e in part)))
    # last_vals を持ち越し
    last_vals = [initial * p["weight"] for p in parts]
    idxs = [0] * len(parts)
    combined = []
    for ts in all_ts:
        for i, part in enumerate(part_eqs):
            # part[idxs[i]] の ts がこれ以下である最新のもの
            while idxs[i] < len(part) - 1 and pd.Timestamp(part[idxs[i] + 1]["ts"]) <= ts:
                idxs[i] += 1
            if idxs[i] < len(part):
                last_vals[i] = part[idxs[i]]["equity"]
        combined.append({"ts": ts, "equity": round(sum(last_vals), 2)})

    return R43.summarize(combined, initial, n_trades=total_trades)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 実行
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def tag(r):
    t = []
    if r["all_positive"]: t.append("🎯毎年+")
    elif r["no_negative"]: t.append("🟢ﾏｲﾅｽ無")
    if r["max_dd"] < 20: t.append("🛡DD<20%!")
    elif r["max_dd"] < 30: t.append("🛡DD<30%")
    elif r["max_dd"] < 40: t.append("◯DD<40%")
    if r["avg_annual_ret"] >= 50: t.append("⭐+50%")
    elif r["avg_annual_ret"] >= 30: t.append("💪+30%")
    elif r["avg_annual_ret"] >= 15: t.append("🌱+15%")
    if r["sharpe"] >= 1.5: t.append("⚡Sharpe優")
    elif r["sharpe"] >= 1.0: t.append("◯Sharpe良")
    return " ".join(t)


def main():
    print("=" * 135)
    print("🛡️ Iter45: 低DD特化 — DD 30%以下を目指す 10パターン比較")
    print("=" * 135)
    all_data = load_data()
    start, end = "2020-01-01", "2024-12-31"

    runs = [
        ("D01 3分割 BTC/ETH/USDT (33/33/34)",
         lambda: run_diversified(all_data, start, end,
                                 {"BTC/USDT":0.33, "ETH/USDT":0.33, "USDT":0.34})),
        ("D02 BTC/USDT 50/50 (超保守)",
         lambda: run_diversified(all_data, start, end,
                                 {"BTC/USDT":0.50, "USDT":0.50})),
        ("D03 BTC/ETH/USDT 25/25/50 (現金厚め)",
         lambda: run_diversified(all_data, start, end,
                                 {"BTC/USDT":0.25, "ETH/USDT":0.25, "USDT":0.50})),
        ("D04 BTCマイルド(EMA50) + USDT金利",
         lambda: run_btc_strict(all_data, start, end, cash_rate=0.03, ema_col="ema50")),
        ("D05 モメンタムTop3 + DD上限-15%",
         lambda: run_momentum_dd_capped(all_data, start, end, top_n=3, dd_trigger_pct=15.0)),
        ("D05b モメンタムTop3 + DD上限-10% 厳",
         lambda: run_momentum_dd_capped(all_data, start, end, top_n=3, dd_trigger_pct=10.0)),
        ("D06 モメンタム25%+BTCマイルド25%+USDT50%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_momentum,  "weight": 0.25, "kwargs":{"top_n":3,"lookback_days":90}},
             {"fn": R43.run_btc_mild,  "weight": 0.25, "kwargs":{"cash_rate":0.03}},
             {"weight": 0.50, "usdt_rate": 0.03},
         ])),
        ("D07 BTCマイルド60%+モメンタム40%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.60, "kwargs":{"cash_rate":0.03}},
             {"fn": R43.run_momentum,  "weight": 0.40, "kwargs":{"top_n":3,"lookback_days":90}},
         ])),
        ("D08 静ハイブリッド 70%BTC+30%モメンタム",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.70, "kwargs":{"cash_rate":0.03}},
             {"fn": R43.run_momentum,  "weight": 0.30, "kwargs":{"top_n":3,"lookback_days":90}},
         ])),
        ("D09 究極分散 BTC20%+ETH20%+モメ20%+USDT40%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": (lambda a,s,e,initial,**kw: run_diversified(a,s,e,{"BTC/USDT":1.0})), "weight": 0.20, "kwargs": {}},
             {"fn": (lambda a,s,e,initial,**kw: run_diversified(a,s,e,{"ETH/USDT":1.0})), "weight": 0.20, "kwargs": {}},
             {"fn": R43.run_momentum,  "weight": 0.20, "kwargs":{"top_n":3,"lookback_days":90}},
             {"weight": 0.40, "usdt_rate": 0.03},
         ])),
        ("D10 超保守 BTCマイルド30%+USDT70%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.30, "kwargs":{"cash_rate":0.03}},
             {"weight": 0.70, "usdt_rate": 0.03},
         ])),
    ]

    print(f"\n{'No':4s} | {'戦略':46s} | {'20':>5s} {'21':>6s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'★DD':>6s} | {'Sh':>4s} | 判定")
    print("-" * 145)

    results = {}
    for name, fn in runs:
        t0 = time.time()
        r = fn()
        row = f"{name[:4]} | {name:46s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+4.1f}% "[:7]
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>5.1f}% | "
        row += f"{r['sharpe']:>4.2f} | "
        row += tag(r)
        print(row, flush=True)
        results[name] = r

    # ベスト選定
    low_dd = [(n, r) for n, r in results.items() if r["max_dd"] < 30]
    best_dd30 = max(low_dd, key=lambda x: x[1]["avg_annual_ret"]) if low_dd else None
    low_dd20 = [(n, r) for n, r in results.items() if r["max_dd"] < 20]
    best_dd20 = max(low_dd20, key=lambda x: x[1]["avg_annual_ret"]) if low_dd20 else None
    all_positive = [(n, r) for n, r in results.items() if r["no_negative"]]
    best_pos = max(all_positive, key=lambda x: x[1]["avg_annual_ret"]) if all_positive else None

    print("\n" + "=" * 135)
    if best_dd20:
        n, r = best_dd20
        print(f"🛡️🛡️ DD<20% ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    if best_dd30:
        n, r = best_dd30
        print(f"🛡️ DD<30% ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    if best_pos:
        n, r = best_pos
        print(f"🎯 毎年プラス達成: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    print("=" * 135)

    out = {
        "results": results,
        "best_dd30": best_dd30[0] if best_dd30 else None,
        "best_dd20": best_dd20[0] if best_dd20 else None,
        "best_positive": best_pos[0] if best_pos else None,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 追加: さらに超保守パターン (D11〜D15)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main_ultra_safe():
    """究極の低DDを追求する追加5パターン"""
    print("=" * 135)
    print("🛡️🛡️🛡️ Iter45 PLUS: 究極の低DD追求 (目標DD<25%)")
    print("=" * 135)
    all_data = load_data()
    start, end = "2020-01-01", "2024-12-31"

    runs = [
        ("D11 BTC/USDT 25/75 (現金75%)",
         lambda: run_diversified(all_data, start, end, {"BTC/USDT":0.25, "USDT":0.75})),
        ("D12 BTC/ETH/USDT 10/10/80 (現金80%)",
         lambda: run_diversified(all_data, start, end, {"BTC/USDT":0.10, "ETH/USDT":0.10, "USDT":0.80})),
        ("D13 BTCマイルド20%+USDT80%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.20, "kwargs":{"cash_rate":0.03}},
             {"weight": 0.80, "usdt_rate": 0.03},
         ])),
        ("D14 BTCマイルド10%+モメ10%+USDT80%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.10, "kwargs":{"cash_rate":0.03}},
             {"fn": R43.run_momentum,  "weight": 0.10, "kwargs":{"top_n":3,"lookback_days":90}},
             {"weight": 0.80, "usdt_rate": 0.03},
         ])),
        ("D15 BTCマイルド15%+USDT85%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.15, "kwargs":{"cash_rate":0.03}},
             {"weight": 0.85, "usdt_rate": 0.03},
         ])),
        ("D16 モメンタム15%+BTCマイルド15%+USDT70%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_momentum,  "weight": 0.15, "kwargs":{"top_n":3,"lookback_days":90}},
             {"fn": R43.run_btc_mild,  "weight": 0.15, "kwargs":{"cash_rate":0.03}},
             {"weight": 0.70, "usdt_rate": 0.03},
         ])),
        ("D17 BTCマイルド25%+モメ15%+USDT60%",
         lambda: run_multi_portfolio(all_data, start, end, [
             {"fn": R43.run_btc_mild,  "weight": 0.25, "kwargs":{"cash_rate":0.03}},
             {"fn": R43.run_momentum,  "weight": 0.15, "kwargs":{"top_n":3,"lookback_days":90}},
             {"weight": 0.60, "usdt_rate": 0.03},
         ])),
    ]

    print(f"\n{'No':4s} | {'戦略':46s} | {'20':>5s} {'21':>6s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'★DD':>6s} | {'Sh':>4s} | 判定")
    print("-" * 145)

    results = {}
    for name, fn in runs:
        r = fn()
        row = f"{name[:4]} | {name:46s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+4.1f}% "[:7]
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>5.1f}% | "
        row += f"{r['sharpe']:>4.2f} | "
        row += tag(r)
        print(row, flush=True)
        results[name] = r

    # 既存のIter45 JSONに追加
    existing = json.loads(OUT_PATH.read_text())
    existing["results"].update(results)

    # 新しいベスト計算
    all_results = existing["results"]
    low_dd = [(n, r) for n, r in all_results.items() if r["max_dd"] < 30]
    best_dd30 = max(low_dd, key=lambda x: x[1]["avg_annual_ret"]) if low_dd else None
    low_dd25 = [(n, r) for n, r in all_results.items() if r["max_dd"] < 25]
    best_dd25 = max(low_dd25, key=lambda x: x[1]["avg_annual_ret"]) if low_dd25 else None
    low_dd20 = [(n, r) for n, r in all_results.items() if r["max_dd"] < 20]
    best_dd20 = max(low_dd20, key=lambda x: x[1]["avg_annual_ret"]) if low_dd20 else None
    pos = [(n, r) for n, r in all_results.items() if r["no_negative"]]
    best_pos = max(pos, key=lambda x: x[1]["avg_annual_ret"]) if pos else None

    existing["best_dd30"] = best_dd30[0] if best_dd30 else None
    existing["best_dd25"] = best_dd25[0] if best_dd25 else None
    existing["best_dd20"] = best_dd20[0] if best_dd20 else None
    existing["best_positive"] = best_pos[0] if best_pos else None

    print("\n" + "=" * 135)
    if best_dd20:
        n, r = best_dd20
        print(f"🛡️🛡️🛡️ DD<20% ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    if best_dd25:
        n, r = best_dd25
        print(f"🛡️🛡️ DD<25% ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    if best_dd30:
        n, r = best_dd30
        print(f"🛡️ DD<30% ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / Sharpe {r['sharpe']}")
    if best_pos:
        n, r = best_pos
        print(f"🎯 毎年プラス達成: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}%")
    print("=" * 135)

    OUT_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__" and "--ultra" in sys.argv:
    main_ultra_safe()
