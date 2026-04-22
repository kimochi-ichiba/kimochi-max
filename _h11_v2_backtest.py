"""
H11 v2 バックテスト (リファクタ版)
=====================================
前回の教訓:
  - 頻繁な売買は税金と手数料で必ず負ける
  - 税金は「全資産から按分」で支払う必要がある
  - 2020-2024 BTC +13x の強気相場ではガチホが圧倒的に強い
  - 勝つ可能性があるのは「大きなDDを避けつつ、99%はガチホに近い動きをする」戦略

今回のv2の設計方針:
  - BTC を主軸 (90% 配分)、ACHとUSDTはサブ (10%)
  - トレンド保護: 3条件 (EMA200割れ + RSI<35 + ATR収縮) 全て満たすときだけ売却
  - 再エントリー: EMA200復活 + RSI>45
  - 出ている間は USDT を USDE/Aave で 8% 利回り
  - 年1-3回の取引に抑えて税金・手数料を最小化
  - 税金は「売却時のキャッシュから直接」差し引く
"""
from __future__ import annotations
import sys, json, pickle
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategy_h11_v2 import (
    H11V2Config, detect_regime, compute_allocation,
    select_momentum_candidates, diversify_by_correlation,
    rsi, ema, atr,
)

PROJECT = Path(__file__).resolve().parent
CACHE = PROJECT / "results/_cache_alldata.pkl"
OUT_JSON = PROJECT / "results/h11_v2_backtest.json"

# ━━━ 前提 ━━━
INITIAL_USD = 26_700.0
MONTHLY_DEPOSIT_USD = 1_333.0
FEE = 0.0006
SLIP = 0.0003
START = pd.Timestamp("2020-01-01")
END = pd.Timestamp("2024-12-30")
USD_JPY = 150.0

# 税率
SHORT_TAX = 0.55  # 雑所得 (短期)
LONG_TAX = 0.20   # 2028年以降の分離課税想定 (長期)


def load_data():
    with open(CACHE, "rb") as f:
        data = pickle.load(f)
    return {sym.replace("/USDT", ""): df for sym, df in data.items()}


def monthly_deposit_trigger(d, state):
    """月が変わったら True を返す"""
    key = (d.year, d.month)
    if state.get("_last_deposit_month") != key:
        state["_last_deposit_month"] = key
        return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 戦略A: BTC ガチホ (月次積立, 最終日に全売却で課税)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_hold(btc_df, dates):
    qty = 0.0
    lots = []  # (qty, entry_price, entry_date)
    total_deposit = INITIAL_USD
    equity_hist = []

    p0 = float(btc_df.loc[dates[0], "close"]) * (1 + SLIP)
    qty0 = INITIAL_USD / p0 * (1 - FEE)
    qty = qty0
    lots.append((qty0, p0, dates[0]))

    state = {"_last_deposit_month": (dates[0].year, dates[0].month)}

    for d in dates:
        price = float(btc_df.loc[d, "close"])
        if monthly_deposit_trigger(d, state):
            buy = price * (1 + SLIP)
            add_qty = MONTHLY_DEPOSIT_USD / buy * (1 - FEE)
            qty += add_qty
            lots.append((add_qty, buy, d))
            total_deposit += MONTHLY_DEPOSIT_USD
        equity = qty * price
        equity_hist.append({"date": d, "equity": equity})

    # 最終清算
    final_d = dates[-1]
    final_p = float(btc_df.loc[final_d, "close"]) * (1 - SLIP)
    gross_proceeds = qty * final_p * (1 - FEE)

    tax = 0.0
    total_pnl = 0.0
    for (lot_qty, lot_price, lot_date) in lots:
        lot_proceeds = lot_qty * final_p * (1 - FEE)
        lot_cost = lot_qty * lot_price
        lot_pnl = lot_proceeds - lot_cost
        total_pnl += lot_pnl
        if lot_pnl > 0:
            hold_days = (final_d - lot_date).days
            rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
            tax += lot_pnl * rate

    return {
        "name": "A: BTCガチホ",
        "equity_history": equity_hist,
        "total_deposited": total_deposit,
        "final_gross": gross_proceeds,
        "final_net": gross_proceeds - tax,
        "tax_total": tax,
        "trades_count": len(lots),
        "max_dd_pct": compute_max_dd(equity_hist),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 戦略B: H11 v1 (シンプル版・現行ロジック)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_v1(btc_df, data, dates):
    """
    現行H11: BTC40%(EMA200) + ACH40%(月次Top3 90日) + USDT20%(年3%)
    税金は売却時のキャッシュから直接差し引く。
    """
    # 単一プール管理 (現金で全て表現)
    cash = INITIAL_USD  # cashは全通貨相当を一括で管理
    # ただし配分用に論理ポケットは持つ
    btc_bucket_target = 0.40
    ach_bucket_target = 0.40
    usdt_bucket_target = 0.20

    # 初期配分
    btc_cash = INITIAL_USD * btc_bucket_target
    ach_cash = INITIAL_USD * ach_bucket_target
    usdt_cash = INITIAL_USD * usdt_bucket_target
    btc_qty = 0.0
    btc_entry = 0.0
    btc_entry_date = None
    ach_positions = {}

    last_rebalance = None
    total_deposit = INITIAL_USD
    realized_per_year = {}  # year -> gross realized pnl
    tax_total = 0.0
    trades = 0
    equity_hist = []
    state = {"_last_deposit_month": (dates[0].year, dates[0].month)}

    for d in dates:
        btc_price = float(btc_df.loc[d, "close"])
        ema200 = btc_df.loc[d].get("ema200", np.nan)

        # 月次積立 (各枠に按分)
        if monthly_deposit_trigger(d, state):
            btc_cash += MONTHLY_DEPOSIT_USD * btc_bucket_target
            ach_cash += MONTHLY_DEPOSIT_USD * ach_bucket_target
            usdt_cash += MONTHLY_DEPOSIT_USD * usdt_bucket_target
            total_deposit += MONTHLY_DEPOSIT_USD

        # BTC ロジック
        if pd.notna(ema200):
            if btc_price > ema200 and btc_qty == 0 and btc_cash > 100:
                buy = btc_price * (1 + SLIP)
                btc_qty = btc_cash / buy * (1 - FEE)
                btc_entry = buy
                btc_entry_date = d
                btc_cash = 0
                trades += 1
            elif btc_price < ema200 and btc_qty > 0:
                sell = btc_price * (1 - SLIP)
                proceeds = btc_qty * sell * (1 - FEE)
                pnl = proceeds - btc_qty * btc_entry
                btc_cash = proceeds
                if pnl > 0:
                    hold_days = (d - btc_entry_date).days
                    rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                    tax = pnl * rate
                    btc_cash -= tax
                    tax_total += tax
                    realized_per_year[d.year] = realized_per_year.get(d.year, 0) + pnl
                btc_qty = 0
                btc_entry = 0
                trades += 1

        # ACH 月次リバランス (30日)
        if last_rebalance is None or (d - last_rebalance).days >= 30:
            # 全決済
            proceeds_total = ach_cash
            for sym, pos in list(ach_positions.items()):
                if d not in data[sym].index:
                    continue
                cur = float(data[sym].loc[d, "close"])
                sell = cur * (1 - SLIP)
                p = pos["qty"] * sell * (1 - FEE)
                pnl = p - pos["qty"] * pos["entry_price"]
                proceeds_total += p
                if pnl > 0:
                    hold_days = (d - pos["entry_date"]).days
                    rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                    tax = pnl * rate
                    proceeds_total -= tax
                    tax_total += tax
                    realized_per_year[d.year] = realized_per_year.get(d.year, 0) + pnl
                trades += 1
            ach_positions = {}
            ach_cash = proceeds_total

            # Bear時はACHスキップ
            if pd.notna(ema200) and btc_price > ema200:
                # Top3 モメンタム (90日)
                cand = []
                for sym, df in data.items():
                    if d not in df.index or len(df) < 95:
                        continue
                    idx = df.index.get_loc(d)
                    if idx < 90:
                        continue
                    r = (float(df["close"].iloc[idx]) / float(df["close"].iloc[idx-90]) - 1) * 100
                    cand.append((sym, r, float(df["close"].iloc[idx])))
                cand.sort(key=lambda x: x[1], reverse=True)
                top3 = cand[:3]
                if top3 and ach_cash > 100:
                    per = ach_cash / len(top3)
                    for sym, ret, price in top3:
                        buy = price * (1 + SLIP)
                        qty = per / buy * (1 - FEE)
                        ach_positions[sym] = {"qty": qty, "entry_price": buy, "entry_date": d}
                        trades += 1
                    ach_cash = 0
            last_rebalance = d

        # USDT金利 (年3%)
        usdt_cash *= (1 + (1.03 ** (1/365) - 1))

        # エクイティ集計
        ach_val = ach_cash
        for sym, pos in ach_positions.items():
            if d in data[sym].index:
                ach_val += pos["qty"] * float(data[sym].loc[d, "close"])
        btc_val = btc_cash + btc_qty * btc_price
        total = btc_val + ach_val + usdt_cash
        equity_hist.append({"date": d, "equity": total})

    # 最終含み益を課税
    final_d = dates[-1]
    final_net = equity_hist[-1]["equity"]
    if btc_qty > 0:
        final_p = float(btc_df.loc[final_d, "close"]) * (1 - SLIP)
        proceeds = btc_qty * final_p * (1 - FEE)
        pnl = proceeds - btc_qty * btc_entry
        if pnl > 0:
            hold_days = (final_d - btc_entry_date).days
            rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
            tax = pnl * rate
            final_net -= tax
            tax_total += tax
    for sym, pos in ach_positions.items():
        if final_d in data[sym].index:
            final_p = float(data[sym].loc[final_d, "close"]) * (1 - SLIP)
            proceeds = pos["qty"] * final_p * (1 - FEE)
            pnl = proceeds - pos["qty"] * pos["entry_price"]
            if pnl > 0:
                hold_days = (final_d - pos["entry_date"]).days
                rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                tax = pnl * rate
                final_net -= tax
                tax_total += tax

    return {
        "name": "B: H11 v1 (現行)",
        "equity_history": equity_hist,
        "total_deposited": total_deposit,
        "final_gross": equity_hist[-1]["equity"],
        "final_net": final_net,
        "tax_total": tax_total,
        "trades_count": trades,
        "max_dd_pct": compute_max_dd(equity_hist),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 戦略C: H11 v2 (シンプル版・「ほぼガチホ + 熊回避」)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_v2(btc_df, data, dates, variant="v2"):
    """
    H11 v2/v3: 【ほぼガチホ + 熊回避 + USDT@8%】

    v2 (保守版):
      - 売却条件 3つ全て: EMA200割れ + RSI14<35 + 過去20日リターン<-15%
      - 買い戻し: EMA200 > 7日前 + RSI>45

    v3 (攻め版, 早期撤退):
      - 売却条件 (いずれか):
          (a) 20日リターン < -30% (クラッシュ警報)
          (b) 価格 < EMA200 × 0.90 (持続的弱さ)
      - 買い戻し (全て): 価格 > EMA200 + EMA200が5日前より上昇 + RSI>50
      - ACHは四半期リバランス (60日) で税金削減
    """
    # BTC用事前計算
    btc_prep = btc_df.copy()
    btc_prep["rsi14"] = rsi(btc_prep["close"], 14)
    btc_prep["ema200"] = ema(btc_prep["close"], 200)
    btc_prep["ret20"] = btc_prep["close"].pct_change(20)

    # 設定
    BTC_TARGET = 0.90
    ACH_TARGET = 0.10
    USDT_RATE = 0.08  # USDE/Aave想定

    # プール
    btc_cash = INITIAL_USD * BTC_TARGET
    ach_cash = INITIAL_USD * ACH_TARGET
    usdt_cash_parked = 0.0   # BTC撤退中の待機資金 (年8%)
    btc_qty = 0.0
    btc_entry = 0.0
    btc_entry_date = None
    btc_in_market = False
    ach_positions = {}

    last_ach_rebalance = None
    total_deposit = INITIAL_USD
    tax_total = 0.0
    trades = 0
    equity_hist = []
    state = {"_last_deposit_month": (dates[0].year, dates[0].month)}

    # 初日 BTC 購入
    first_d = dates[0]
    first_p = float(btc_prep.loc[first_d, "close"])
    buy = first_p * (1 + SLIP)
    btc_qty = btc_cash / buy * (1 - FEE)
    btc_entry = buy
    btc_entry_date = first_d
    btc_cash = 0
    btc_in_market = True
    trades += 1

    cfg = H11V2Config()
    cfg.ach_rebalance_days = 60 if variant == "v3" else 30
    cfg.ach_top_n = 3

    for d in dates:
        row = btc_prep.loc[d]
        btc_price = float(row["close"])
        ema200 = row.get("ema200", np.nan)
        r14 = row.get("rsi14", np.nan)
        ret20 = row.get("ret20", np.nan)

        # 月次積立
        if monthly_deposit_trigger(d, state):
            if btc_in_market:
                # BTC保有中 → BTCに追加
                buy = btc_price * (1 + SLIP)
                add_qty = (MONTHLY_DEPOSIT_USD * BTC_TARGET) / buy * (1 - FEE)
                # 平均取得価格更新
                new_cost = btc_qty * btc_entry + (MONTHLY_DEPOSIT_USD * BTC_TARGET)
                btc_qty += add_qty
                btc_entry = new_cost / btc_qty if btc_qty > 0 else buy
            else:
                # 待機中 → USDT@8%に追加
                usdt_cash_parked += MONTHLY_DEPOSIT_USD * BTC_TARGET
            # ACH部分
            ach_cash += MONTHLY_DEPOSIT_USD * ACH_TARGET
            total_deposit += MONTHLY_DEPOSIT_USD

        # BTC 保護ロジック
        if pd.notna(ema200) and pd.notna(r14) and pd.notna(ret20):
            if btc_in_market:
                # 売却判定 (variant で切替)
                if variant == "v3":
                    # 攻め版: (a) 20日リタ < -30% OR (b) 価格 < EMA200 × 0.90
                    should_sell = (ret20 < -0.30) or (btc_price < ema200 * 0.90)
                else:
                    # 保守版: 3条件すべて
                    should_sell = btc_price < ema200 and r14 < 35 and ret20 < -0.15
                if should_sell:
                    sell = btc_price * (1 - SLIP)
                    proceeds = btc_qty * sell * (1 - FEE)
                    pnl = proceeds - btc_qty * btc_entry
                    cash_after_tax = proceeds
                    if pnl > 0:
                        hold_days = (d - btc_entry_date).days
                        rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                        tax = pnl * rate
                        cash_after_tax -= tax
                        tax_total += tax
                    usdt_cash_parked += cash_after_tax  # 待機USDTへ
                    btc_qty = 0
                    btc_entry = 0
                    btc_in_market = False
                    trades += 1
            else:
                # 買い戻し判定
                ema_now = ema200
                idx = btc_prep.index.get_loc(d)
                lookback_days = 5 if variant == "v3" else 7
                if idx >= lookback_days:
                    ema_prev = btc_prep["ema200"].iloc[idx-lookback_days]
                    rsi_thresh = 50 if variant == "v3" else 45
                    if pd.notna(ema_prev) and btc_price > ema_now and ema_now > ema_prev and r14 > rsi_thresh:
                        if usdt_cash_parked > 100:
                            buy = btc_price * (1 + SLIP)
                            btc_qty = usdt_cash_parked / buy * (1 - FEE)
                            btc_entry = buy
                            btc_entry_date = d
                            usdt_cash_parked = 0
                            btc_in_market = True
                            trades += 1

        # ACH リバランス (30日)
        if last_ach_rebalance is None or (d - last_ach_rebalance).days >= 30:
            # 決済
            proceeds_total = ach_cash
            for sym, pos in list(ach_positions.items()):
                if d not in data[sym].index:
                    continue
                cur = float(data[sym].loc[d, "close"])
                sell = cur * (1 - SLIP)
                p = pos["qty"] * sell * (1 - FEE)
                pnl = p - pos["qty"] * pos["entry_price"]
                proceeds_total += p
                if pnl > 0:
                    hold_days = (d - pos["entry_date"]).days
                    rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                    tax = pnl * rate
                    proceeds_total -= tax
                    tax_total += tax
                trades += 1
            ach_positions = {}
            ach_cash = proceeds_total

            # Bear regime なら ACH も USDT に待機
            if btc_in_market:
                try:
                    cands = select_momentum_candidates(data, d, cfg)
                    sel = diversify_by_correlation(cands, data, d, cfg)
                    if sel and ach_cash > 50:
                        per = ach_cash / len(sel)
                        for c in sel:
                            price = c["price"]
                            buy = price * (1 + SLIP)
                            qty = per / buy * (1 - FEE)
                            ach_positions[c["symbol"]] = {"qty": qty, "entry_price": buy, "entry_date": d}
                            trades += 1
                        ach_cash = 0
                except Exception:
                    pass
            last_ach_rebalance = d

        # 待機USDTとACH現金の金利 (年8%)
        daily_rate = (1 + USDT_RATE) ** (1/365) - 1
        usdt_cash_parked *= (1 + daily_rate)
        ach_cash *= (1 + daily_rate)

        # エクイティ集計
        total = btc_qty * btc_price + usdt_cash_parked + ach_cash
        for sym, pos in ach_positions.items():
            if d in data[sym].index:
                total += pos["qty"] * float(data[sym].loc[d, "close"])
        equity_hist.append({"date": d, "equity": total,
                           "btc_in_market": btc_in_market})

    # 最終清算課税
    final_d = dates[-1]
    final_net = equity_hist[-1]["equity"]
    if btc_qty > 0:
        final_p = float(btc_prep.loc[final_d, "close"]) * (1 - SLIP)
        proceeds = btc_qty * final_p * (1 - FEE)
        pnl = proceeds - btc_qty * btc_entry
        if pnl > 0:
            hold_days = (final_d - btc_entry_date).days
            rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
            tax = pnl * rate
            final_net -= tax
            tax_total += tax
    for sym, pos in ach_positions.items():
        if final_d in data[sym].index:
            final_p = float(data[sym].loc[final_d, "close"]) * (1 - SLIP)
            proceeds = pos["qty"] * final_p * (1 - FEE)
            pnl = proceeds - pos["qty"] * pos["entry_price"]
            if pnl > 0:
                hold_days = (final_d - pos["entry_date"]).days
                rate = LONG_TAX if hold_days >= 365 else SHORT_TAX
                tax = pnl * rate
                final_net -= tax
                tax_total += tax

    return {
        "name": f"C: H11 {variant} (新)",
        "equity_history": equity_hist,
        "total_deposited": total_deposit,
        "final_gross": equity_hist[-1]["equity"],
        "final_net": final_net,
        "tax_total": tax_total,
        "trades_count": trades,
        "max_dd_pct": compute_max_dd(equity_hist),
    }


def compute_max_dd(equity_hist):
    """最大ドローダウン% を計算"""
    peak = 0
    max_dd = 0
    for e in equity_hist:
        v = e["equity"]
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)


def main():
    print("📥 データロード中...")
    data = load_data()
    btc_df = data["BTC"]
    dates = [d for d in btc_df.index if START <= d <= END]
    print(f"   期間: {dates[0].date()} 〜 {dates[-1].date()} ({len(dates)}日)")

    print("\n🏃 A: BTCガチホ...")
    a = run_hold(btc_df, dates)
    print(f"   最終 (税引前/税引後): ${a['final_gross']:,.0f} / ${a['final_net']:,.0f}")
    print(f"   税: ${a['tax_total']:,.0f} | 最大DD: {a['max_dd_pct']}% | 取引: {a['trades_count']}")

    print("\n🏃 B: H11 v1 (現行)...")
    b = run_v1(btc_df, data, dates)
    print(f"   最終 (税引前/税引後): ${b['final_gross']:,.0f} / ${b['final_net']:,.0f}")
    print(f"   税: ${b['tax_total']:,.0f} | 最大DD: {b['max_dd_pct']}% | 取引: {b['trades_count']}")

    print("\n🏃 C: H11 v2 (保守版・熊回避)...")
    c = run_v2(btc_df, data, dates, variant="v2")
    print(f"   最終 (税引前/税引後): ${c['final_gross']:,.0f} / ${c['final_net']:,.0f}")
    print(f"   税: ${c['tax_total']:,.0f} | 最大DD: {c['max_dd_pct']}% | 取引: {c['trades_count']}")

    print("\n🏃 D: H11 v3 (攻め版・早期撤退)...")
    e = run_v2(btc_df, data, dates, variant="v3")
    print(f"   最終 (税引前/税引後): ${e['final_gross']:,.0f} / ${e['final_net']:,.0f}")
    print(f"   税: ${e['tax_total']:,.0f} | 最大DD: {e['max_dd_pct']}% | 取引: {e['trades_count']}")

    def thin(hist):
        return [
            {"date": str(h["date"])[:10], "equity": round(h["equity"], 2)}
            for i, h in enumerate(hist) if i % 7 == 0
        ]

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"start": str(dates[0].date()), "end": str(dates[-1].date()), "days": len(dates)},
        "initial_usd": INITIAL_USD,
        "monthly_deposit_usd": MONTHLY_DEPOSIT_USD,
        "usd_jpy": USD_JPY,
        "strategies": {
            "hold": {**{k: v for k, v in a.items() if k != "equity_history"},
                     "equity_history": thin(a["equity_history"])},
            "v1":   {**{k: v for k, v in b.items() if k != "equity_history"},
                     "equity_history": thin(b["equity_history"])},
            "v2":   {**{k: v for k, v in c.items() if k != "equity_history"},
                     "equity_history": thin(c["equity_history"])},
            "v3":   {**{k: v for k, v in e.items() if k != "equity_history"},
                     "equity_history": thin(e["equity_history"])},
        },
    }
    OUT_JSON.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n💾 {OUT_JSON}")

    print("\n" + "=" * 75)
    print(f"🏆 最終サマリー (2020-2024, 投入総額 ¥{a['total_deposited']*USD_JPY:,.0f})")
    print("=" * 75)
    for label, r in [("A: ガチホ", a), ("B: v1現行", b), ("C: v2保守", c), ("D: v3攻め", e)]:
        net = r["final_net"]
        yen = net * USD_JPY
        multiple = net / r["total_deposited"]
        print(f"{label:12s}  税引後 ¥{yen:>13,.0f}  ({multiple:>5.2f}倍)  "
              f"最大DD {r['max_dd_pct']:>5.1f}%  取引 {r['trades_count']:>4}回")
    print("=" * 75)


if __name__ == "__main__":
    main()
