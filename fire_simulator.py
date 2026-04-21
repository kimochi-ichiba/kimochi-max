"""
FIRE計画シミュレーター
========================
28歳からスタートして35歳で月40万円不労所得を目指す計画を、
複数の自動売買ボット戦略でシミュレーションする。

ユーザー計画:
  - 初期: 400万円
  - 月次積立: 20万円 × 7年 (84ヶ月)
  - 総投入額: 2,080万円
  - 目標: 7年後に1.5〜2億円 (7-10倍、年率32-38%)

比較戦略:
  A. 現行計画 (年率30%、一般的な想定)
  B. 気持ちマックス ハイブリッド (バックテスト年率+54.8%、DD 39.7%)
  C. 気持ちマックス Pro 強化版 (年率+70%狙い、DD 50%想定)
  D. モメンタムTop3 (バックテスト年率+130%、DD 69%)
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from datetime import datetime

OUT_JSON = Path("/Users/sanosano/projects/kimochi-max/results/fire_simulation.json")

# ユーザー計画
INITIAL_CAPITAL = 4_000_000      # 初期資金 400万円
MONTHLY_DEPOSIT = 200_000        # 月次積立 20万円
YEARS = 7                        # 28歳〜35歳
MONTHS = YEARS * 12              # 84ヶ月
USER_AGE_START = 28
USER_AGE_END = 35

# FIRE後の運用
POST_FIRE_ANNUAL_RATE = 0.04     # VYM等の年率4%想定
TAX_RATE_CRYPTO_2028 = 0.20      # 2028年以降の分離課税 20% (予定)
TAX_RATE_STOCK = 0.20315         # 株式配当税率


def simulate_monthly_compound(initial, monthly, annual_rate, months, dd_schedule=None):
    """
    月次積立 + 年利複利 のシミュレーション

    Args:
        initial: 初期資金 (円)
        monthly: 月次積立額 (円)
        annual_rate: 年利 (decimal, 例: 0.55 = 55%)
        months: 運用月数
        dd_schedule: オプション [(month, dd_pct), ...]
            特定月に DDが発生するシナリオ
    Returns:
        月別残高リスト
    """
    monthly_rate = (1 + annual_rate) ** (1/12) - 1
    balance = initial
    history = [balance]
    for m in range(1, months + 1):
        balance = balance * (1 + monthly_rate) + monthly
        # DDシナリオ適用
        if dd_schedule:
            for dd_month, dd_pct in dd_schedule:
                if m == dd_month:
                    balance *= (1 - dd_pct / 100)
        history.append(round(balance))
    return history


def simulate_simple_compound(initial, annual_rate, years):
    """単純な複利（積立なし）"""
    return initial * ((1 + annual_rate) ** years)


def tax_on_crypto(gain, rate=0.55):
    """
    仮想通貨 利益に対する税金
    - 2027年まで: 総合課税 最大55%
    - 2028年以降: 分離課税 20% (予定)
    """
    return gain * rate


def calc_fire_income(capital, annual_rate=POST_FIRE_ANNUAL_RATE, tax_rate=TAX_RATE_STOCK):
    """FIRE後の年間手取り配当金"""
    gross = capital * annual_rate
    net = gross * (1 - tax_rate)
    return {
        "gross_annual": round(gross),
        "net_annual": round(net),
        "monthly": round(net / 12),
    }


def main():
    print("=" * 80)
    print("🔥 FIRE計画シミュレーター - 28歳→35歳 月40万円不労所得を目指して")
    print("=" * 80)
    print()
    print(f"📋 前提条件:")
    print(f"   初期資金: {INITIAL_CAPITAL:,}円")
    print(f"   月次積立: {MONTHLY_DEPOSIT:,}円")
    print(f"   期間: {YEARS}年 ({MONTHS}ヶ月)")
    print(f"   総投入額: {INITIAL_CAPITAL + MONTHLY_DEPOSIT * MONTHS:,}円")
    print()

    total_invested = INITIAL_CAPITAL + MONTHLY_DEPOSIT * MONTHS

    # 戦略パラメータ
    strategies = [
        {
            "key": "user_plan",
            "name": "📝 あなたの元計画",
            "annual_rate": 0.30,
            "description": "仮想通貨を現状ポートフォリオで運用、想定年率+30%",
            "dd_schedule": [(48, 40)],  # 32歳ごろ (BTC 4年サイクル) に-40%
            "risk_level": "middle",
        },
        {
            "key": "h11_hybrid",
            "name": "💎 気持ちマックス ハイブリッド (バックテスト検証済み)",
            "annual_rate": 0.548,  # バックテスト実績
            "description": "BTC40%+ACH40%+USDT20%、DD 39.7%検証済み",
            "dd_schedule": [(30, 40)],  # 中期に1回-40%のDD想定
            "risk_level": "middle-high",
        },
        {
            "key": "h11_pro",
            "name": "🚀 気持ちマックス Pro 強化版 (提案)",
            "annual_rate": 0.70,  # レバレッジ段階強化で狙う
            "description": "気持ちマックス + 動的レバレッジ + 月次リバランス + 目標到達時の段階利確",
            "dd_schedule": [(30, 50)],  # より大きいDD -50%想定
            "risk_level": "high",
        },
        {
            "key": "momentum_top3",
            "name": "🔥 モメンタムTop3 (超攻め)",
            "annual_rate": 1.30,  # バックテスト年率+130%
            "description": "50銘柄の上位3銘柄を毎月選定、DD 69%の覚悟必要",
            "dd_schedule": [(30, 70)],  # DD 70%の年
            "risk_level": "very-high",
        },
    ]

    results = []
    for s in strategies:
        # DDあり シナリオ
        h_with_dd = simulate_monthly_compound(
            INITIAL_CAPITAL, MONTHLY_DEPOSIT, s["annual_rate"], MONTHS, s["dd_schedule"])
        # DDなし (理想シナリオ)
        h_ideal = simulate_monthly_compound(
            INITIAL_CAPITAL, MONTHLY_DEPOSIT, s["annual_rate"], MONTHS, None)

        final_with_dd = h_with_dd[-1]
        final_ideal = h_ideal[-1]

        gain_with_dd = final_with_dd - total_invested
        gain_ideal = final_ideal - total_invested

        # 税金計算 (2028年以降を想定)
        tax_with_dd = tax_on_crypto(gain_with_dd, TAX_RATE_CRYPTO_2028)
        tax_ideal = tax_on_crypto(gain_ideal, TAX_RATE_CRYPTO_2028)
        net_with_dd = final_with_dd - tax_with_dd
        net_ideal = final_ideal - tax_ideal

        # FIRE後の月額
        fire_with_dd = calc_fire_income(net_with_dd)
        fire_ideal = calc_fire_income(net_ideal)

        result = {
            **s,
            "monthly_history_with_dd": h_with_dd,
            "monthly_history_ideal": h_ideal,
            "final_with_dd": final_with_dd,
            "final_ideal": final_ideal,
            "total_invested": total_invested,
            "gain_with_dd": gain_with_dd,
            "gain_ideal": gain_ideal,
            "multiplier_with_dd": round(final_with_dd / total_invested, 2),
            "multiplier_ideal": round(final_ideal / total_invested, 2),
            "tax_with_dd": tax_with_dd,
            "tax_ideal": tax_ideal,
            "net_with_dd": net_with_dd,
            "net_ideal": net_ideal,
            "fire_monthly_with_dd": fire_with_dd["monthly"],
            "fire_monthly_ideal": fire_ideal["monthly"],
            "fire_gross_annual_with_dd": fire_with_dd["gross_annual"],
        }
        results.append(result)

        print(f"━━━ {s['name']} (年率 {s['annual_rate']*100:.0f}%) ━━━")
        print(f"  35歳時の資産額:")
        print(f"    理想シナリオ : ¥{final_ideal:>12,.0f} ({final_ideal/total_invested:.2f}倍)")
        print(f"    DD考慮シナリオ: ¥{final_with_dd:>12,.0f} ({final_with_dd/total_invested:.2f}倍)")
        print(f"    税引き後     : ¥{net_with_dd:>12,.0f}")
        print(f"  FIRE後 (年利4% VYM等):")
        print(f"    月々の配当金 : ¥{fire_with_dd['monthly']:>10,.0f} / 月")
        print()

    # ユーザーが目標とする「月40万円」達成に必要な最低資産額
    required_capital_for_40man = (40 * 10000 * 12) / (POST_FIRE_ANNUAL_RATE * (1 - TAX_RATE_STOCK))
    print(f"🎯 月40万円 FIRE達成に必要な税引き後資産:")
    print(f"   = (月40万 × 12) / (年利4% × 税引き0.79685)")
    print(f"   = ¥{required_capital_for_40man:,.0f}")
    print()

    for r in results:
        ok = r["net_with_dd"] >= required_capital_for_40man
        star = "✅ 達成可能" if ok else "❌ 未達"
        print(f"  {star}  {r['name']:35s} → ¥{r['net_with_dd']:>12,.0f} (目標: ¥{required_capital_for_40man:,.0f})")
    print()

    # JSON出力
    out = {
        "generated_at": datetime.now().isoformat(),
        "inputs": {
            "initial_capital": INITIAL_CAPITAL,
            "monthly_deposit": MONTHLY_DEPOSIT,
            "years": YEARS,
            "months": MONTHS,
            "total_invested": total_invested,
            "age_start": USER_AGE_START,
            "age_end": USER_AGE_END,
            "post_fire_annual_rate": POST_FIRE_ANNUAL_RATE,
            "tax_crypto_2028": TAX_RATE_CRYPTO_2028,
            "tax_stock": TAX_RATE_STOCK,
        },
        "fire_target_monthly": 400_000,
        "required_capital_for_40man": round(required_capital_for_40man),
        "strategies": results,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
