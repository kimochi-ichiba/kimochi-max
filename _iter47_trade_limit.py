"""
Iter47: 1日取引上限バックテスト比較 (5回/10回/20回/無制限)
==============================================================
既存の H11 ハイブリッド (BTC40%+ACH40%+USDT20%) を、
日次取引上限4パターンで動かし、結果を比較する。

背景:
  - ACH月次リバランス時は 全決済3 + 新規購入3 = 最大6件 必要
  - 現行5回上限はリバランス日の6件目をブロック
  - 20回上限が適切か、無制限版と比較してバックテストで確認

出力: results/iter47_trade_limit.json
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
import _iter43_rethink as R43

CACHE_PATH = Path("/Users/sanosano/projects/kimochi-max/results/_cache_alldata.pkl")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter47_trade_limit.json")

FEE = 0.0006
SLIP = 0.0003


def load_data():
    with open(CACHE_PATH, "rb") as f:
        return pickle.load(f)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# run_momentum 拡張版: 日次取引上限対応
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_momentum_limited(all_data, start, end, top_n=3, lookback_days=90,
                          initial=10_000.0, max_daily_trades=None,
                          external_trade_counts=None):
    """
    ACH Top3 モメンタム (日次取引上限付き)

    Args:
        max_daily_trades: 1日あたりの取引上限 (None=無制限)
        external_trade_counts: H11ハイブリッド用 defaultdict(int)
                                 外部(BTC枠)取引数と合算して上限チェック
    Returns:
        {..., 'blocked_count': N, 'daily_counts': {...}}
    """
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    blocked = 0
    last_rebalance_month = None
    daily_counts = external_trade_counts if external_trade_counts is not None else defaultdict(int)
    blocked_events = []

    def can_trade(date_str):
        if max_daily_trades is None:
            return True
        return daily_counts[date_str] < max_daily_trades

    for date in dates:
        date_str = str(date)[:10]

        # リバランス判定
        rebalance = (last_rebalance_month is None) or (date.month != last_rebalance_month)

        if rebalance:
            # ━ 全決済フェーズ ━
            total = cash
            # 保有中ポジションの時価
            mark_prices = {}
            for sym, qty in positions.items():
                df = all_data[sym]
                if date in df.index:
                    mark_prices[sym] = df.loc[date, "close"]
                    total += qty * mark_prices[sym]
            # cashに全部確定 (SELL扱い)
            for sym in list(positions.keys()):
                if sym not in mark_prices:
                    continue
                if can_trade(date_str):
                    price = mark_prices[sym] * (1 - SLIP)
                    cash += positions[sym] * price * (1 - FEE)
                    daily_counts[date_str] += 1
                    n_trades += 1
                    positions.pop(sym)
                else:
                    blocked += 1
                    blocked_events.append({
                        "date": date_str, "sym": sym, "action": "SELL",
                        "reason": f"daily limit {max_daily_trades} reached"
                    })
                    # 売れなかったのでポジション保持したまま。cashには入らない。

            # 売れた分だけ cash が増え、positions から除外される
            # 売れなかった分は positions に残る

            # ━ Top3選定 ━
            scores = []
            for sym, df in all_data.items():
                if date not in df.index: continue
                past_idx = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
                if len(past_idx) < 20: continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past_idx[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20: continue
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)

            # BTCレジームチェック
            btc_r = btc_df.loc[date]
            btc_price_btc = btc_r["close"]; btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_price_btc < btc_ema200:
                # Bear: 買わない
                last_rebalance_month = date.month
            else:
                selected = scores[:top_n]
                if selected:
                    # 売れ残りポジションがない新規買い枠ぶんだけ現金を配分
                    # 既に保有中の銘柄が次回も選ばれたらそのまま保持
                    new_syms = [s for s, _ in selected if s not in positions]
                    if new_syms:
                        weight = 1.0 / len(new_syms)
                        for sym in new_syms:
                            if can_trade(date_str):
                                df = all_data[sym]
                                price = df.loc[date, "close"] * (1 + SLIP)
                                cost = cash * weight
                                if cost > 0:
                                    qty = cost / price * (1 - FEE)
                                    positions[sym] = qty
                                    cash -= cost
                                    daily_counts[date_str] += 1
                                    n_trades += 1
                            else:
                                blocked += 1
                                blocked_events.append({
                                    "date": date_str, "sym": sym, "action": "BUY",
                                    "reason": f"daily limit {max_daily_trades} reached"
                                })
                last_rebalance_month = date.month

        # 時価評価
        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["blocked_count"] = blocked
    r["blocked_events"] = blocked_events[:50]  # 先頭50件
    r["daily_counts_top10"] = dict(sorted(daily_counts.items(),
                                            key=lambda x: -x[1])[:10])
    return r


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# run_btc_mild 拡張版: 日次取引上限対応
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_btc_mild_limited(all_data, start, end, initial=10_000.0, cash_rate=0.03,
                          max_daily_trades=None, external_trade_counts=None):
    """BTCマイルド EMA200戦略 (日次上限付き)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial; qty = 0.0; position = False
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0; blocked = 0; prev_date = None
    daily_counts = external_trade_counts if external_trade_counts is not None else defaultdict(int)

    for date in dates:
        date_str = str(date)[:10]
        r = btc_df.loc[date]
        price = r["close"]
        ema = r.get("ema200")
        if not position and prev_date is not None:
            days = (date - prev_date).days
            cash *= (1 + cash_rate) ** (days / 365)
        prev_date = date

        if pd.isna(ema):
            equity_curve.append({"ts": date, "equity": cash + qty * price})
            continue

        def can_trade():
            if max_daily_trades is None:
                return True
            return daily_counts[date_str] < max_daily_trades

        if price > ema and not position:
            if can_trade():
                buy_price = price * (1 + SLIP)
                qty = cash / buy_price * (1 - FEE)
                cash = 0; position = True
                daily_counts[date_str] += 1
                n_trades += 1
            else:
                blocked += 1
        elif price < ema and position:
            if can_trade():
                sell_price = price * (1 - SLIP)
                cash = qty * sell_price * (1 - FEE)
                qty = 0; position = False
                daily_counts[date_str] += 1
                n_trades += 1
            else:
                blocked += 1

        equity_curve.append({"ts": date, "equity": cash + qty * price})

    res = R43.summarize(equity_curve, initial, n_trades=n_trades)
    res["blocked_count"] = blocked
    return res


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# H11 ハイブリッド (BTC40%+ACH40%+USDT20%) 合算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_h11_hybrid_limited(all_data, start, end, initial=10_000.0,
                             max_daily_trades=None):
    """H11: BTC40%+ACH40%+USDT20%, 日次上限共有"""
    btc_weight = 0.40
    ach_weight = 0.40
    usdt_weight = 0.20

    # 共通の取引数カウンター (BTC枠とACH枠で共有)
    shared_counts = defaultdict(int)

    # BTC枠
    btc_res = run_btc_mild_limited(
        all_data, start, end,
        initial=initial * btc_weight, cash_rate=0.03,
        max_daily_trades=max_daily_trades,
        external_trade_counts=shared_counts,
    )
    # ACH枠
    ach_res = run_momentum_limited(
        all_data, start, end, top_n=3, lookback_days=90,
        initial=initial * ach_weight,
        max_daily_trades=max_daily_trades,
        external_trade_counts=shared_counts,
    )
    # USDT枠 (金利のみ、取引なし)
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    usdt_bal = initial * usdt_weight
    usdt_hist = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": usdt_bal}]
    prev_d = dates[0]
    for d in dates:
        days = (d - prev_d).days
        usdt_bal *= (1 + 0.03) ** (days / 365)
        prev_d = d
        usdt_hist.append({"ts": d, "equity": usdt_bal})

    # 合算
    btc_map = {e["ts"]: e["equity"] for e in btc_res["equity_weekly"]}
    ach_map = {e["ts"]: e["equity"] for e in ach_res["equity_weekly"]}
    usdt_map = {}
    usdt_df = pd.DataFrame(usdt_hist).set_index("ts")
    usdt_df.index = pd.to_datetime(usdt_df.index)
    usdt_weekly = usdt_df.resample("W").last().dropna()
    for d, v in usdt_weekly["equity"].items():
        usdt_map[str(d)[:10]] = float(v)

    # 全日付を合算
    all_ts = sorted(set(list(btc_map.keys()) + list(ach_map.keys()) + list(usdt_map.keys())))
    combined = []
    last_btc = initial * btc_weight
    last_ach = initial * ach_weight
    last_usdt = initial * usdt_weight
    for ts in all_ts:
        if ts in btc_map: last_btc = btc_map[ts]
        if ts in ach_map: last_ach = ach_map[ts]
        if ts in usdt_map: last_usdt = usdt_map[ts]
        combined.append({"ts": ts, "equity": round(last_btc + last_ach + last_usdt, 2)})

    total_final = combined[-1]["equity"]
    total_trades = btc_res["n_trades"] + ach_res["n_trades"]
    total_blocked = btc_res.get("blocked_count", 0) + ach_res.get("blocked_count", 0)

    # 集計 (summarize使えない: 組合せなので自前計算)
    eq_df = pd.DataFrame(combined).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    prev_eq = initial
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        ye = float(yr.iloc[-1])
        yearly[y] = round((ye / prev_eq - 1) * 100, 2) if prev_eq > 0 else 0
        prev_eq = ye
    peak, max_dd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    weekly_ret = eq_df["equity"].pct_change().dropna()
    sharpe = float(weekly_ret.mean() / weekly_ret.std() * np.sqrt(52)) if weekly_ret.std() > 0 else 0

    # リバランス完全実行率
    daily_counts = dict(shared_counts)
    rebalance_days = [k for k, v in daily_counts.items() if v >= 3]  # 3件以上=リバランス日相当
    full_rebalance_days = [k for k in rebalance_days if daily_counts[k] >= 6]
    full_exec_rate = len(full_rebalance_days) / max(len(rebalance_days), 1) * 100 if rebalance_days else 0

    return {
        "max_daily_trades": max_daily_trades,
        "final": round(total_final, 2),
        "total_ret": round((total_final - initial) / initial * 100, 2),
        "avg_annual_ret": round(((total_final / initial) ** (1/5) - 1) * 100, 2),
        "yearly": yearly,
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "n_trades": total_trades,
        "n_blocked": total_blocked,
        "n_trades_btc": btc_res["n_trades"],
        "n_trades_ach": ach_res["n_trades"],
        "n_blocked_btc": btc_res.get("blocked_count", 0),
        "n_blocked_ach": ach_res.get("blocked_count", 0),
        "full_rebalance_days": len(full_rebalance_days),
        "total_rebalance_days": len(rebalance_days),
        "full_rebalance_rate_pct": round(full_exec_rate, 2),
        "top10_heavy_days": dict(sorted(daily_counts.items(), key=lambda x: -x[1])[:10]),
        "equity_weekly": combined,
        "blocked_events_sample": ach_res.get("blocked_events", [])[:20],
    }


def main():
    print("=" * 110)
    print("🧪 Iter47: H11 ハイブリッド 1日取引上限 比較バックテスト")
    print("=" * 110)
    print("  戦略: BTC40%(EMA200) + ACH40%(Top3モメンタム) + USDT20%(金利3%)")
    print("  期間: 2020-01-01 〜 2024-12-31, 初期資金 $10,000")
    print()

    all_data = load_data()
    patterns = [
        ("A. 5回 (現行制限)", 5),
        ("B. 10回", 10),
        ("C. 20回 (推奨)", 20),
        ("D. 無制限", None),
    ]

    print(f"{'パターン':30s} | {'最終資産':>12s} | {'年率':>6s} | {'DD':>5s} | {'Sharpe':>6s} | {'取引':>4s} | {'ﾌﾞﾛｯｸ':>5s} | {'ﾘﾊﾞﾗﾝｽ完全':>8s}")
    print("-" * 110)
    results = []
    for name, limit in patterns:
        t0 = time.time()
        r = run_h11_hybrid_limited(all_data, "2020-01-01", "2024-12-31",
                                     max_daily_trades=limit)
        r["pattern_name"] = name
        elapsed = time.time() - t0
        print(f"{name:30s} | ${r['final']:>10,.0f} | {r['avg_annual_ret']:>+5.1f}% | "
              f"{r['max_dd']:>4.1f}% | {r['sharpe']:>5.2f} | {r['n_trades']:>3d} | "
              f"{r['n_blocked']:>4d} | "
              f"{r['full_rebalance_days']}/{r['total_rebalance_days']} "
              f"({r['full_rebalance_rate_pct']:.0f}%)")
        results.append(r)

    # 5回と無制限の差
    base_final = results[0]["final"]    # 5回
    free_final = results[-1]["final"]   # 無制限
    diff_pct = (free_final - base_final) / base_final * 100
    print("\n" + "=" * 110)
    print(f"📊 5回上限 vs 無制限の最終資産差: {diff_pct:+.2f}% "
          f"(${free_final - base_final:+,.0f})")
    print(f"   5回で失われた取引機会: {results[0]['n_blocked']}件")
    print(f"   20回でブロック件数: {results[2]['n_blocked']}件 "
          f"(≈ 無制限と同等か検証)")
    print("=" * 110)

    out = {
        "generated_at": datetime.now().isoformat(),
        "initial": 10_000,
        "patterns": results,
        "recommendation": "20" if results[2]["n_blocked"] <= 1 else "更に引き上げ検討",
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
