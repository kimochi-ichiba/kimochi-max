"""R55バックテストの算数・ロジック監査"""
import json
from pathlib import Path

p = (Path(__file__).resolve().parent / "results" / "last_push.json")
if not p.exists():
    p = (Path(__file__).resolve().parent / "results" / "final_push.json")
data = json.load(open(p))

print("=" * 100)
print("🔍 R55 検証データ監査")
print("=" * 100)

# R55 または同等の毎年+戦略を抽出
for name, r in data.items():
    if r.get("all_positive") and r.get("avg_annual_ret", 0) >= 100:
        print(f"\n【戦略】 {name}")
        yearly = r.get("yearly", {})
        print(f"年別リターン:")
        for y in sorted(yearly.keys()):
            print(f"  {y}: {yearly[y]:+.2f}%")

        # 各年の（1 + リターン小数）を掛け算
        compound = 1.0
        for y in sorted(yearly.keys()):
            compound *= (1 + yearly[y] / 100)
        compound_pct = (compound - 1) * 100

        print(f"\n📐 算数チェック:")
        print(f"  年別リターンの複利積: {compound:.4f} = {compound_pct:+.2f}%")
        print(f"  報告された 5年計(total_ret): {r.get('total_ret', 'N/A'):+.2f}%")
        print(f"  報告された 年率(avg_annual): {r.get('avg_annual_ret', 'N/A'):+.2f}%")
        print(f"  年率から逆算した 5年計: {(((1 + r['avg_annual_ret']/100)**5) - 1) * 100:+.2f}%")

        # 整合性
        diff = r.get('total_ret', 0) - compound_pct
        if abs(diff) > 100:
            print(f"  ⚠️ 乖離 {diff:+.1f}pp — 計算不整合あり")
        else:
            print(f"  ✅ 乖離 {diff:+.1f}pp")

        # 最終残高再計算
        print(f"\n💰 $10,000 からのシミュ:")
        eq = 10_000
        for y in sorted(yearly.keys()):
            eq *= (1 + yearly[y] / 100)
            print(f"  {y}末: ${eq:>12,.0f}")
        reported_final = 10_000 * (1 + r.get('total_ret', 0)/100)
        print(f"  → 複利計算値: ${eq:,.0f}")
        print(f"  → 報告値:     ${reported_final:,.0f}")
        print(f"  → 差: ${reported_final - eq:+,.0f}")

        # DD とトレード情報
        print(f"\n📊 その他検証項目:")
        print(f"  最大DD: {r.get('max_dd', 'N/A'):.1f}%")
        print(f"  総取引数: {r.get('n_trades', 'N/A')}")
        print(f"  LONG取引: {r.get('n_long', 'N/A')}")
        print(f"  SHORT取引: {r.get('n_short', 'N/A')}")
        print(f"  勝率: {r.get('win_rate', 'N/A'):.1f}%")
        print(f"  銀行ロック残: ${r.get('locked_bank', 0):,.2f}")

        # SHORT検証（2022年に何回SHORTしたか）
        if r.get('n_short', 0) == 0 and yearly.get(2022, 0) > 0:
            print(f"  ⚠️ 2022年プラスなのに SHORT=0 → 疑問: SHORTなしで BTC年-65% の相場でプラス?")

        print("-" * 100)

# 全戦略の整合性チェック
print(f"\n📋 全戦略の整合性:")
print(f"{'戦略':50s} | {'複利値':>10s} | {'報告値':>10s} | 乖離")
print("-" * 100)
for name, r in data.items():
    yearly = r.get("yearly", {})
    if not yearly: continue
    comp = 1.0
    for v in yearly.values():
        comp *= (1 + v/100)
    comp_pct = (comp - 1) * 100
    rep = r.get('total_ret', 0)
    diff = rep - comp_pct
    flag = "⚠️" if abs(diff) > 50 else "✅"
    print(f"{name:50s} | {comp_pct:>+8.1f}% | {rep:>+8.1f}% | {diff:+7.1f}pp {flag}")
