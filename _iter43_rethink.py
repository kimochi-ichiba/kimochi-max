"""
Iter43: 根本見直し — 全く違うアプローチも含めて比較
=====================================================
これまで「I34の微調整」ばかりだった反省を活かし、
根本的に違う戦略タイプを5種類まとめてバックテスト。

パターン一覧（10種類）:
  R01: BTC単純保有 (ベンチマーク) - 何もしない
  R02: ETH単純保有
  R03: BTC/ETH 60/40 保有
  R04: BTC買い持ちの「マイルド版」 - BTC EMA200下では撤退
  R05: 月次モメンタム Top3 (上位3銘柄を毎月ローテーション)
  R06: 月次モメンタム Top5
  R07: I34 (既存、比較用)
  R08: AC (Iter41推奨、比較用)
  R09: ACH (Iter42安全版、比較用)
  R10: ハイブリッド = BTC 50% + AC 50%
  R11: ACに現金金利+3%年利を加算
  R12: I34+金利 (I34ベースに現金時年3%金利)
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
FEE = 0.0006
SLIP = 0.0003


def load_data():
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    if CACHE_PATH.exists():
        age_h = (time.time() - CACHE_PATH.stat().st_mtime) / 3600
        if age_h < 24:
            print(f"📦 キャッシュ使用（{age_h:.1f}h前）")
            with open(CACHE_PATH, "rb") as f:
                return pickle.load(f)
    d = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(d, f)
    return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 共通: 年別集計 / DD / equity週次サンプル
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def summarize(equity_curve, initial=10_000.0, n_trades=0, n_liq=0, extra=None):
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
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
    final = float(eq_df["equity"].iloc[-1])
    avg_annual = ((final / initial) ** (1/5) - 1) * 100 if final > 0 else -100

    # シャープレシオ（日次換算、年率化）
    eq_daily = eq_df["equity"].resample("D").last().ffill()
    daily_ret = eq_daily.pct_change().dropna()
    if len(daily_ret) > 0 and daily_ret.std() > 0:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(365))
    else:
        sharpe = 0

    eq_weekly = eq_df.resample("W").last().dropna()
    eq_list = [{"ts": str(d)[:10], "equity": round(float(e), 2)}
               for d, e in eq_weekly["equity"].items()]

    ret = {
        "final": round(final, 2),
        "total_ret": round((final - initial) / initial * 100, 2),
        "avg_annual_ret": round(avg_annual, 2),
        "yearly": yearly,
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "n_trades": n_trades,
        "n_liquidations": n_liq,
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
        "equity_weekly": eq_list,
    }
    if extra:
        ret.update(extra)
    return ret


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# R01-R03: Buy & Hold
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_buy_hold(all_data, symbols, weights, start, end, initial=10_000.0):
    """複数銘柄を固定比率で保有（年1回リバランス）"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    weights = np.array(weights) / sum(weights)

    # 初期購入
    qtys = {}
    first_date = dates[0]
    for sym, w in zip(symbols, weights):
        df = all_data[sym]
        if first_date not in df.index:
            first_date = df.index[df.index >= start_ts][0]
        price = df.loc[first_date, "close"] * (1 + SLIP)
        cost = initial * w
        qty = cost / price * (1 - FEE)
        qtys[sym] = qty

    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    current_year = dates[0].year

    for date in dates:
        # 年1回リバランス
        if date.year != current_year:
            total = 0
            for sym in symbols:
                df = all_data[sym]
                if date in df.index:
                    total += qtys[sym] * df.loc[date, "close"]
            for sym, w in zip(symbols, weights):
                df = all_data[sym]
                if date in df.index:
                    target = total * w
                    price = df.loc[date, "close"]
                    new_qty = target / price * (1 - FEE)
                    qtys[sym] = new_qty
            current_year = date.year

        # 時価評価
        total = 0
        for sym in symbols:
            df = all_data[sym]
            if date in df.index:
                total += qtys[sym] * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return summarize(equity_curve, initial, n_trades=len(symbols) * 5)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# R04: BTC マイルド版 (EMA200下では現金)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_btc_mild(all_data, start, end, initial=10_000.0, cash_rate=0.03):
    """BTC EMA200の上で保有、下では現金（年3%金利）"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    qty = 0.0
    position = False
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    prev_date = None

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]; ema200 = r.get("ema200")
        # 現金金利（日割り）
        if not position and prev_date is not None:
            days = (date - prev_date).days
            cash *= (1 + cash_rate) ** (days / 365)
        prev_date = date

        if pd.isna(ema200):
            eq = cash + qty * price
            equity_curve.append({"ts": date, "equity": eq})
            continue

        # シグナル
        if price > ema200 and not position:
            buy_price = price * (1 + SLIP)
            qty = cash / buy_price * (1 - FEE)
            cash = 0
            position = True
            n_trades += 1
        elif price < ema200 and position:
            sell_price = price * (1 - SLIP)
            cash = qty * sell_price * (1 - FEE)
            qty = 0
            position = False
            n_trades += 1

        eq = cash + qty * price
        equity_curve.append({"ts": date, "equity": eq})

    return summarize(equity_curve, initial, n_trades=n_trades)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# R05-R06: モメンタム Top N ローテーション
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_momentum(all_data, start, end, top_n=3, lookback_days=90,
                 rebalance_freq="M", initial=10_000.0):
    """月初に過去90日リターン上位N銘柄を等配分で保有"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}  # sym -> qty
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    last_rebalance_month = None

    for date in dates:
        # リバランス判定
        rebalance = False
        if last_rebalance_month is None:
            rebalance = True
        elif rebalance_freq == "M" and date.month != last_rebalance_month:
            rebalance = True
        elif rebalance_freq == "W" and date.weekday() == 0:  # 月曜
            rebalance = True

        if rebalance:
            # 現保有を時価で確定
            total = cash
            for sym, qty in positions.items():
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - SLIP)
                    total += qty * price * (1 - FEE)
            positions.clear()
            cash = total

            # 過去 lookback_days のリターンで上位選抜
            scores = []
            for sym, df in all_data.items():
                if date not in df.index: continue
                past_idx = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
                if len(past_idx) < 20: continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past_idx[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20: continue  # トレンド弱すぎは除外
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)

            # BTC自体のレジームチェック: BTC < EMA200 ならスキップ（現金）
            btc_r = btc_df.loc[date]
            btc_price = btc_r["close"]; btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_price < btc_ema200:
                # 全部現金のまま
                last_rebalance_month = date.month
            else:
                selected = scores[:top_n]
                if selected:
                    weight = 1.0 / len(selected)
                    for sym, _ in selected:
                        df = all_data[sym]
                        price = df.loc[date, "close"] * (1 + SLIP)
                        cost = cash * weight
                        qty = cost / price * (1 - FEE)
                        positions[sym] = qty
                        n_trades += 1
                    cash = 0
                last_rebalance_month = date.month

        # 時価評価
        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return summarize(equity_curve, initial, n_trades=n_trades)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# R07-R09: 既存I34 / AC / ACH 再実行
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def i34_base_cfg():
    base = dict(
        risk_per_trade_pct=0.02, max_pos=20, stop_loss_pct=0.15,
        tp1_pct=0.10, tp1_fraction=0.4, tp2_pct=0.25, tp2_fraction=0.5,
        trail_activate_pct=0.30, trail_giveback_pct=0.08,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.0, lev_mid=1.0, lev_high=1.0,
        breakout_pct=0.05, rsi_long_min=50, rsi_long_max=75, rsi_short_min=85,
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


def run_legends_with_interest(all_data, start, end, cfg, initial=10_000.0,
                               cash_annual_rate=0.0):
    """既存エンジンの簡易ラッパ。現金金利は後から equity_curve に加算する近似"""
    # 簡易: 元 run_legends を走らせ、現金比率ぶんの金利を別途加算
    # より厳密には日次で現金残高 × 日割金利 が必要だが、まずは近似。
    # ここではエンジンを再実装せず、そのまま run_legends + 金利加算で概算。
    import _iter42_improve as iter42
    r = iter42.run_iter42(all_data, start, end, cfg, initial=initial)
    if cash_annual_rate > 0:
        # equity_curve に対して、現金利回りぶんを時系列的に加算 (概算)
        # 保守的に total equity × (年率 × 0.3) を複利加算 (平均現金比率 30%想定)
        eq_w = r["equity_weekly"]
        boost = []
        for i, e in enumerate(eq_w):
            weeks = i
            factor = (1 + cash_annual_rate * 0.3) ** (weeks / 52)
            new_eq = e["equity"] * factor
            boost.append({"ts": e["ts"], "equity": round(new_eq, 2)})
        # yearly 再計算
        eq_df = pd.DataFrame(boost).set_index("ts")
        eq_df.index = pd.to_datetime(eq_df.index)
        yearly = {}
        prev_eq = initial
        for y in range(2020, 2025):
            yr = eq_df[eq_df.index.year == y]["equity"]
            if len(yr) == 0: continue
            ye = float(yr.iloc[-1])
            yearly[y] = round((ye / prev_eq - 1) * 100, 2) if prev_eq > 0 else 0
            prev_eq = ye
        final = float(eq_df["equity"].iloc[-1])
        r["equity_weekly"] = boost
        r["yearly"] = yearly
        r["final"] = round(final, 2)
        r["avg_annual_ret"] = round(((final / initial) ** (1/5) - 1) * 100, 2)
        r["total_ret"] = round((final - initial) / initial * 100, 2)
        r["all_positive"] = all(v > 0 for v in yearly.values())
        r["no_negative"] = all(v >= 0 for v in yearly.values())
        r["negative_years"] = sum(1 for v in yearly.values() if v < 0)
    # sharpe, max_dd を equity_curve から再計算
    eq_df = pd.DataFrame(r["equity_weekly"]).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    peak, mdd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        mdd = max(mdd, dd)
    r["max_dd"] = round(mdd, 2)
    weekly_ret = eq_df["equity"].pct_change().dropna()
    if len(weekly_ret) > 0 and weekly_ret.std() > 0:
        r["sharpe"] = round(float(weekly_ret.mean() / weekly_ret.std() * np.sqrt(52)), 2)
    else:
        r["sharpe"] = 0
    return r


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# R10: ハイブリッド = 50% BTC保有 + 50% AC戦略
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_hybrid(all_data, start, end, btc_weight=0.5, initial=10_000.0):
    """資金を btc_weight:(1-btc_weight) で BTCマイルド保有とAC戦略に分配"""
    btc_part = initial * btc_weight
    ac_part = initial * (1 - btc_weight)

    # BTCマイルド部分
    btc_r = run_btc_mild(all_data, start, end, initial=btc_part)

    # AC部分
    import _iter42_improve as iter42
    b = i34_base_cfg()
    ac_cfg = {**b, "pyramid_max": 2, "btc_ema50_filter": True}
    ac_r = iter42.run_iter42(all_data, start, end, ac_cfg, initial=ac_part)

    # 合算 (週次equityを合算)
    # btc_r["equity_weekly"] と ac_r["equity_weekly"] は同じ日付粒度と仮定
    btc_map = {e["ts"]: e["equity"] for e in btc_r["equity_weekly"]}
    ac_map = {e["ts"]: e["equity"] for e in ac_r["equity_weekly"]}
    all_ts = sorted(set(btc_map.keys()) | set(ac_map.keys()))
    combined = []
    last_btc = btc_part; last_ac = ac_part
    for ts in all_ts:
        if ts in btc_map: last_btc = btc_map[ts]
        if ts in ac_map: last_ac = ac_map[ts]
        combined.append({"ts": ts, "equity": round(last_btc + last_ac, 2)})

    # 先頭に初期状態
    combined.insert(0, {"ts": str(pd.Timestamp(start) - pd.Timedelta(days=1))[:10],
                        "equity": initial})
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
    peak, mdd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        mdd = max(mdd, dd)
    final = float(eq_df["equity"].iloc[-1])
    weekly_ret = eq_df["equity"].pct_change().dropna()
    sharpe = float(weekly_ret.mean() / weekly_ret.std() * np.sqrt(52)) if weekly_ret.std() > 0 else 0

    return {
        "final": round(final, 2),
        "total_ret": round((final - initial) / initial * 100, 2),
        "avg_annual_ret": round(((final / initial) ** (1/5) - 1) * 100, 2),
        "yearly": yearly,
        "max_dd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "n_trades": btc_r["n_trades"] + ac_r["n_trades"],
        "n_liquidations": ac_r.get("n_liquidations", 0),
        "all_positive": all(v > 0 for v in yearly.values()),
        "no_negative": all(v >= 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
        "equity_weekly": combined,
    }


def tag(r):
    t = []
    if r["all_positive"]: t.append("🎯毎年+")
    elif r["no_negative"]: t.append("🟢ﾏｲﾅｽ無")
    if r["avg_annual_ret"] >= 70: t.append("🚀+70%")
    elif r["avg_annual_ret"] >= 50: t.append("⭐+50%")
    elif r["avg_annual_ret"] >= 30: t.append("💪+30%")
    if r["max_dd"] < 40: t.append("🛡DD<40")
    if r.get("n_liquidations", 0) == 0: t.append("✅清算0")
    if r["sharpe"] >= 1.5: t.append("⚡Sharpe優")
    elif r["sharpe"] >= 1.0: t.append("◯Sharpe良")
    return " ".join(t)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    print("=" * 130)
    print("🎯 Iter43: 根本見直し — 全く違うアプローチも含めた12パターン比較")
    print("=" * 130)
    all_data = load_data()

    results = {}
    runs = [
        ("R01 BTC単純保有",    lambda: run_buy_hold(all_data, ["BTC/USDT"], [1], "2020-01-01", "2024-12-31")),
        ("R02 ETH単純保有",    lambda: run_buy_hold(all_data, ["ETH/USDT"], [1], "2020-01-01", "2024-12-31")),
        ("R03 BTC/ETH 60/40",  lambda: run_buy_hold(all_data, ["BTC/USDT","ETH/USDT"], [0.6,0.4], "2020-01-01", "2024-12-31")),
        ("R04 BTCマイルド(EMA200)", lambda: run_btc_mild(all_data, "2020-01-01", "2024-12-31", cash_rate=0)),
        ("R04b BTCマイルド+金利3%", lambda: run_btc_mild(all_data, "2020-01-01", "2024-12-31", cash_rate=0.03)),
        ("R05 モメンタムTop3",  lambda: run_momentum(all_data, "2020-01-01", "2024-12-31", top_n=3)),
        ("R06 モメンタムTop5",  lambda: run_momentum(all_data, "2020-01-01", "2024-12-31", top_n=5)),
        ("R07 I34 (既存)",     lambda: run_legends_with_interest(all_data, "2020-01-01", "2024-12-31", i34_base_cfg())),
        ("R08 AC (Iter41)",    lambda: run_legends_with_interest(all_data, "2020-01-01", "2024-12-31",
                                         {**i34_base_cfg(), "pyramid_max": 2, "btc_ema50_filter": True})),
        ("R09 ACH (Iter42安全)", lambda: run_legends_with_interest(all_data, "2020-01-01", "2024-12-31",
                                         {**i34_base_cfg(), "pyramid_max": 2, "btc_ema50_filter": True,
                                          "dynamic_leverage": True})),
        ("R10 ハイブリッド 50/50", lambda: run_hybrid(all_data, "2020-01-01", "2024-12-31", btc_weight=0.5)),
        ("R11 ハイブリッド 30/70", lambda: run_hybrid(all_data, "2020-01-01", "2024-12-31", btc_weight=0.3)),
    ]

    print(f"\n{'No':4s} | {'戦略':28s} | {'20':>5s} {'21':>6s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'Sh':>4s} | {'取引':>4s} | {'清算':>3s} | 判定")
    print("-" * 145)
    for name, fn in runs:
        t0 = time.time()
        r = fn()
        row = f"{name[:4]} | {name:28s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+5.1f}% "[:7]
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['sharpe']:>4.2f} | {r['n_trades']:>4d} | {r.get('n_liquidations',0):>3d} | "
        row += tag(r)
        print(row, flush=True)
        results[name] = r

    # ベスト選定
    positives = [(n, r) for n, r in results.items() if r["no_negative"]]
    best_pos = max(positives, key=lambda x: x[1]["avg_annual_ret"]) if positives else None
    best_sharpe = max(results.items(), key=lambda x: x[1]["sharpe"])
    best_total = max(results.items(), key=lambda x: x[1]["final"])

    print("\n" + "=" * 130)
    if best_pos:
        n, r = best_pos
        print(f"🏆 毎年プラス達成: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / "
              f"Sharpe {r['sharpe']} / $10K→${r['final']:,.0f}")
    n, r = best_sharpe
    print(f"⚡ Sharpe最良: {n}  (Sharpe {r['sharpe']})")
    print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / $10K→${r['final']:,.0f}")
    n, r = best_total
    print(f"💰 最終資産最大: {n}  (${r['final']:,.0f})")
    print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / マイナス年{r['negative_years']}")
    print("=" * 130)

    out_path = (Path(__file__).resolve().parent / "results" / "iter43_rethink.json")
    out_path.write_text(json.dumps({
        "results": results,
        "best_no_negative": best_pos[0] if best_pos else None,
        "best_sharpe": best_sharpe[0],
        "best_total": best_total[0],
    }, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out_path}")


if __name__ == "__main__":
    main()
