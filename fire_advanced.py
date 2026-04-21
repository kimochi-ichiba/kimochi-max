"""
FIRE計画 高度版シミュレーター
=================================
税制3シナリオ × 市場予測3シナリオ × 運用方法3つ で27パターンを計算

税制シナリオ (2026年4月時点の現状):
  - A. 2028年分離課税 20.315% (楽観: 改正案が通る)
  - B. 2030年分離課税 20.315% (中庸: 2-3年遅延)
  - C. 改正なし 総合課税 最大55% (慎重: 政治的に流れる)

市場予測シナリオ (BTC 4年半減期サイクル):
  - 強気: BTC$250K → 暴落 -75% → $200K → 暴落 -70% → 35歳時 $400K
  - 中立: BTC$150K → -65% → $200K → -60% → 35歳時 $300K
  - 弱気: BTC$100K → -55% → $130K → -50% → 35歳時 $200K

運用方法:
  - 現物のみ (BTC/ETH ガチホ)
  - 気持ちマックス Pro ボット (年率+70%目標)
  - ハイブリッド (現物50% + ボット50%)
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime

OUT_JSON = Path("/Users/sanosano/projects/kimochi-max/results/fire_advanced.json")

# 共通入力
INITIAL = 4_000_000
MONTHLY = 200_000
YEARS = 7
MONTHS = YEARS * 12
TOTAL_INVESTED = INITIAL + MONTHLY * MONTHS  # 2080万

# 税制
TAX_BRACKETS = {
    "A_2028_split": {
        "name": "2028年分離課税(楽観)",
        "rate_func": lambda gain, year_after_28: 0.20315 if year_after_28 >= 0 else 0.55,
    },
    "B_2030_split": {
        "name": "2030年分離課税(中庸)",
        "rate_func": lambda gain, year_after_28: 0.20315 if year_after_28 >= 2 else 0.55,
    },
    "C_no_change": {
        "name": "改正なし 総合課税55%(慎重)",
        "rate_func": lambda gain, year_after_28: 0.55,
    },
}

# 市場シナリオ (BTC月次騰落率パターン: 84ヶ月)
def gen_market_scenario(level):
    """
    BTCの月次変動率を生成
    half-cycle: 半減期(2024.4)→ピーク(約18ヶ月後=2026.10) → 弱気1年(2027) → 底(2028) → 半減期(2028) → 次のピーク(2030)
    7年間 (2026.5-2033.5想定): 上昇13ヶ月→ピーク→下落11ヶ月→底→上昇17ヶ月→ピーク→下落14ヶ月→底→...
    """
    months = []
    if level == "bull":
        # 強気: 累積 +1700% (BTC 60K→1100K想定)
        # ピーク: 月+15%×6回, ピーク後: 月-15%×4回, 底: 月+0%×3, 上昇期: 月+10%×6回...
        cycle = [10, 12, 15, 18, 15, 12,   # 上昇加速 (6ヶ月)
                 -8, -15, -20, -18, -10, -5,  # 暴落 (6ヶ月)
                 -2, 0, 2, 4,                 # 底固め (4ヶ月)
                 5, 8, 10, 12, 15, 18, 20, 18, 15, 12, 8, 5,  # 第2サイクル上昇 (12ヶ月)
                 -10, -18, -25, -22, -15, -8, -5,  # 第2暴落 (7ヶ月)
                 0, 2, 4,                          # 底固め (3ヶ月)
                 6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 22, 18, 15, 12, 10, 8, 6, 4]  # 第3上昇
        for i in range(MONTHS):
            months.append(cycle[i % len(cycle)] / 100)
    elif level == "neutral":
        # 中立: 累積 +600%
        cycle = [6, 8, 10, 12, 10, 8,   # 上昇
                 -8, -12, -15, -12, -8, -5,
                 -3, 0, 2,
                 4, 6, 8, 10, 12, 14, 12, 10, 8, 6, 4, 2,
                 -8, -12, -18, -15, -10, -5,
                 0, 2, 4,
                 5, 7, 9, 11, 13, 15, 17, 15, 13, 11, 9, 7, 5, 3]
        for i in range(MONTHS):
            months.append(cycle[i % len(cycle)] / 100)
    else:  # bear
        # 弱気: 累積 +200%
        cycle = [3, 5, 7, 8, 6, 4,
                 -5, -10, -15, -10, -7, -3,
                 -2, 0, 1,
                 2, 4, 6, 8, 10, 8, 6, 4, 2,
                 -8, -15, -20, -15, -10, -5,
                 0, 1, 3,
                 4, 6, 8, 10, 8, 6, 4, 2]
        for i in range(MONTHS):
            months.append(cycle[i % len(cycle)] / 100)
    return months[:MONTHS]


def simulate_buy_hold(market_returns, initial=INITIAL, monthly=MONTHLY):
    """現物ガチホ: 月次でBTCを購入、市場に応じて評価額が変動"""
    balance = initial
    history = [balance]
    invested = initial
    for r in market_returns:
        balance = balance * (1 + r) + monthly
        invested += monthly
        history.append(round(balance))
    return {
        "history": history,
        "final": history[-1],
        "invested": invested,
    }


def simulate_h11_pro(market_returns, initial=INITIAL, monthly=MONTHLY):
    """
    気持ちマックス Pro: 戦略エンジンが市場リターンの一部を取り、リスクを抑える
    - 上昇相場: 市場リターン × 0.85 (一部利確で取り逃すが安全)
    - 下落相場: 市場リターン × 0.4 (defensive、現金化で被害減少)
    - レバ効果: 上昇時 ×1.3 を加算
    """
    balance = initial
    history = [balance]
    for r in market_returns:
        if r > 0:
            effective = r * 0.85 * 1.3   # 1.105
        else:
            effective = r * 0.4   # 暴落の60%を回避
        balance = balance * (1 + effective) + monthly
        history.append(round(balance))
    return {
        "history": history,
        "final": history[-1],
        "invested": initial + monthly * len(market_returns),
    }


def simulate_hybrid(market_returns, initial=INITIAL, monthly=MONTHLY, bot_ratio=0.5):
    """ハイブリッド: 半分現物ガチホ、半分ボット運用"""
    bh_initial = initial * (1 - bot_ratio)
    bot_initial = initial * bot_ratio
    bh_monthly = monthly * (1 - bot_ratio)
    bot_monthly = monthly * bot_ratio

    bh = simulate_buy_hold(market_returns, bh_initial, bh_monthly)
    bot = simulate_h11_pro(market_returns, bot_initial, bot_monthly)
    history = [bh["history"][i] + bot["history"][i] for i in range(len(bh["history"]))]
    return {
        "history": history,
        "final": history[-1],
        "invested": bh["invested"] + bot["invested"],
        "bh_part": bh,
        "bot_part": bot,
    }


def calc_tax(gain, tax_scenario, exit_year_after_2028):
    """利確時の税金計算"""
    rate_func = TAX_BRACKETS[tax_scenario]["rate_func"]
    rate = rate_func(gain, exit_year_after_2028)
    return {
        "tax_rate": rate,
        "tax_amount": gain * rate,
        "net_after_tax": gain - gain * rate,
    }


def main():
    print("=" * 90)
    print("🔥 FIRE計画 高度版シミュレーター")
    print(f"   入力: 初期{INITIAL:,}円 + 月次{MONTHLY:,}円 × {MONTHS}ヶ月 = 投入{TOTAL_INVESTED:,}円")
    print("=" * 90)

    market_scenarios = ["bull", "neutral", "bear"]
    method_scenarios = ["buy_hold", "h11_pro", "hybrid"]
    tax_scenarios = list(TAX_BRACKETS.keys())

    # 市場シナリオ生成
    markets = {s: gen_market_scenario(s) for s in market_scenarios}

    # 各組合せでシミュレーション
    results = {}
    for market in market_scenarios:
        results[market] = {}
        for method in method_scenarios:
            if method == "buy_hold":
                sim = simulate_buy_hold(markets[market])
            elif method == "h11_pro":
                sim = simulate_h11_pro(markets[market])
            else:
                sim = simulate_hybrid(markets[market])
            results[market][method] = sim

    # 税金計算 (35歳時=ほぼ7年後=2033年想定)
    # 2028年からの経過年数 = 2033 - 2028 = 5
    exit_year = 5
    final_results = []
    for market in market_scenarios:
        for method in method_scenarios:
            sim = results[market][method]
            for tax_key in tax_scenarios:
                gain = sim["final"] - sim["invested"]
                tax = calc_tax(gain, tax_key, exit_year)
                fire_capital = sim["final"] - tax["tax_amount"]
                # FIRE後の月次手取り (VYM 4%, 税20.315%)
                monthly_income = fire_capital * 0.04 * (1 - 0.20315) / 12
                final_results.append({
                    "market": market,
                    "method": method,
                    "tax": tax_key,
                    "final_market_value": sim["final"],
                    "invested": sim["invested"],
                    "gain": gain,
                    "tax_rate": tax["tax_rate"],
                    "tax_amount": tax["tax_amount"],
                    "net_after_tax": fire_capital,
                    "fire_monthly": round(monthly_income),
                    "achieves_400k": monthly_income >= 400_000,
                    "history": sim["history"],
                })

    # 結果表示
    print("\n📊 27パターン サマリー (35歳時 月額FIRE収入)")
    print("-" * 90)
    print(f"{'市場':6s} | {'運用法':12s} | {'税制':25s} | {'最終資産':>14s} | {'税引後':>14s} | {'月額':>8s} | 判定")
    print("-" * 90)
    for r in final_results:
        m_jp = {"bull":"強気","neutral":"中立","bear":"弱気"}[r["market"]]
        meth_jp = {"buy_hold":"現物ガチホ","h11_pro":"気持ちマックス Pro","hybrid":"ハイブリッド"}[r["method"]]
        tax_jp = TAX_BRACKETS[r["tax"]]["name"]
        ach = "✅" if r["achieves_400k"] else "❌"
        print(f"{m_jp:4s} | {meth_jp:10s} | {tax_jp:23s} | "
              f"¥{r['final_market_value']:>12,.0f} | ¥{r['net_after_tax']:>12,.0f} | "
              f"¥{r['fire_monthly']:>6,.0f} | {ach}")

    # 推奨パターン抽出
    achievers = [r for r in final_results if r["achieves_400k"]]
    print(f"\n🎯 月40万円達成パターン: {len(achievers)}/27")

    # 中立シナリオでの比較
    print("\n📌 中立(中庸)シナリオでの比較:")
    neut = [r for r in final_results if r["market"] == "neutral"]
    for r in neut:
        meth_jp = {"buy_hold":"現物","h11_pro":"気持ちマックス Pro","hybrid":"ハイブリッド"}[r["method"]]
        tax_jp = {"A_2028_split":"2028分離","B_2030_split":"2030分離","C_no_change":"総合課税55%"}[r["tax"]]
        ach = "✅" if r["achieves_400k"] else "❌"
        print(f"   {meth_jp:10s} × {tax_jp:12s} → 月¥{r['fire_monthly']:>6,} {ach}")

    # 出力
    out = {
        "generated_at": datetime.now().isoformat(),
        "inputs": {
            "initial": INITIAL,
            "monthly": MONTHLY,
            "years": YEARS,
            "months": MONTHS,
            "total_invested": TOTAL_INVESTED,
        },
        "tax_scenarios": {k: TAX_BRACKETS[k]["name"] for k in TAX_BRACKETS},
        "market_scenarios": {
            "bull": "強気: BTC強い半減期サイクル、累積+1700%",
            "neutral": "中立: 標準的サイクル、累積+600%",
            "bear": "弱気: 横ばい寄り、累積+200%",
        },
        "method_scenarios": {
            "buy_hold": "現物ガチホ (BTC/ETH を持ち続けるだけ)",
            "h11_pro": "気持ちマックス Proボット (戦略エンジンで運用)",
            "hybrid": "ハイブリッド (現物50% + ボット50%)",
        },
        "market_returns": {k: v for k, v in markets.items()},
        "results": final_results,
        "achievers_count": len(achievers),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
