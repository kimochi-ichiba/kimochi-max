"""
R04の毎年プラス + R07の+100%年率を両立させる精密反復
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ruin_proof_backtest import run_ruin_proof
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    print("📥 データ取得中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ ({time.time()-t0:.0f}秒)\n")

    base = dict(
        risk_per_trade_pct=0.01, max_pos=3,
        stop_loss_pct=0.04, trail_activate_pct=0.15, trail_giveback_pct=0.04,
        adx_min=45, adx_lev2=50, adx_lev3=60,
        lev_low=1.0, lev_mid=1.5, lev_high=2.0,
        breakout_pct_above_ema200=0.05, enable_short=True,
        year_profit_lock=True, btc_adx_for_short=30,
    )

    configs = [
        # R04 を強化
        ("R04原型: 保守 ADX45 Lev1-2 3pos",            {**base}),
        ("R10 Lev2-3 (R04+高レバ)",                   {**base, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R11 Lev3-4 (R04+超高レバ)",                  {**base, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R12 ADX40で緩和+Lev2-3",
         {**base, "adx_min": 40, "adx_lev2": 48, "adx_lev3": 55,
          "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R13 5pos + Lev2-3",                      {**base, "max_pos": 5, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R14 8pos + Lev2-3",                      {**base, "max_pos": 8, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R15 リスク2% + Lev2-3",
         {**base, "risk_per_trade_pct": 0.02, "max_pos": 5, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R16 利伸ばし強化 (トレール+30%発動)",
         {**base, "trail_activate_pct": 0.30, "trail_giveback_pct": 0.06,
          "max_pos": 5, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0}),
        ("R17 ピラミ感: SL6% リスク1.5% Lev2-3",
         {**base, "stop_loss_pct": 0.06, "risk_per_trade_pct": 0.015,
          "max_pos": 5, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0,
          "trail_activate_pct": 0.25}),
        ("R18 ADX42 Lev2.5-3.5 7pos",
         {**base, "adx_min": 42, "adx_lev2": 48, "adx_lev3": 56, "max_pos": 7,
          "lev_low": 2.5, "lev_mid": 3.0, "lev_high": 3.5}),
    ]

    print(f"{'=' * 145}")
    print(f"🔁 精密反復: R04(毎年+)とR07(+100%)の両立を狙う")
    print(f"{'=' * 145}")
    print(f"{'戦略':50s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>7s} | {'DD':>5s} | 判定")
    print("-" * 145)

    best_both = None
    results = {}
    for name, cfg in configs:
        r = run_ruin_proof(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:50s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r
        if r["all_positive"] and r["avg_annual_ret"] >= 100:
            if best_both is None or r["avg_annual_ret"] > best_both[1]["avg_annual_ret"]:
                best_both = (name, r)

    print(f"\n{'=' * 145}")
    if best_both:
        print(f"🎉🎉 **両条件達成**: {best_both[0]}")
        r = best_both[1]
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}%")
    else:
        # 毎年プラス戦略
        pos = sorted([(n, r) for n, r in results.items() if r["all_positive"]],
                     key=lambda x: -x[1]["avg_annual_ret"])
        if pos:
            print(f"✅ 毎年プラス達成: {len(pos)}戦略（年率順）")
            for n, r in pos[:5]:
                print(f"   🎯 {n}: 年率{r['avg_annual_ret']:+.1f}% / DD{r['max_dd']:.1f}%")
    print(f"{'=' * 145}")

    out = (Path(__file__).resolve().parent / "results" / "refine_iteration.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
