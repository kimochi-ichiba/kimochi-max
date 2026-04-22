"""
最終押し: 毎年プラス + 年+100% 両達成への最終反復
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

    # R15がベース: リスク2% Lev2-3 5pos 毎年+ 年率+71%
    base = dict(
        risk_per_trade_pct=0.02, max_pos=5,
        stop_loss_pct=0.04, trail_activate_pct=0.15, trail_giveback_pct=0.04,
        adx_min=45, adx_lev2=50, adx_lev3=60,
        lev_low=2.0, lev_mid=2.5, lev_high=3.0,
        breakout_pct_above_ema200=0.05, enable_short=True,
        year_profit_lock=True, btc_adx_for_short=30,
    )

    configs = [
        ("R15基準 (参考)",                                   {**base}),
        ("R30 Lev3-4 高レバ強化",                            {**base, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R31 Lev3-5 超高レバ",                             {**base, "lev_low": 3.0, "lev_mid": 4.0, "lev_high": 5.0}),
        ("R32 ADX40+Lev3-4 エントリー機会増",
         {**base, "adx_min": 40, "adx_lev2": 48, "adx_lev3": 56,
          "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R33 SL3% タイト化 + Lev3-4",
         {**base, "stop_loss_pct": 0.03, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R34 リスク3% + Lev3-4",
         {**base, "risk_per_trade_pct": 0.03, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R35 利確+25% + Lev3-4",
         {**base, "trail_activate_pct": 0.25, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R36 3pos集中 + Lev3-4",
         {**base, "max_pos": 3, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R37 利益ロックOFF + Lev3-4",
         {**base, "year_profit_lock": False, "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R38 ADX40+リスク2.5%+Lev3-4",
         {**base, "adx_min": 40, "risk_per_trade_pct": 0.025,
          "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R39 ブレイクアウト3%緩和+Lev3-4",
         {**base, "breakout_pct_above_ema200": 0.03,
          "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
        ("R40 ブレイクアウト8%+Lev3-4",
         {**base, "breakout_pct_above_ema200": 0.08,
          "lev_low": 3.0, "lev_mid": 3.5, "lev_high": 4.0}),
    ]

    print(f"{'=' * 145}")
    print(f"🎯 最終押し: 毎年プラス + 年+100% 両達成探索")
    print(f"{'=' * 145}")
    print(f"{'戦略':50s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>7s} | {'DD':>5s} | 判定")
    print("-" * 145)

    results = {}
    best_both = None
    best_positive_only = None
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
        elif r["avg_annual_ret"] >= 80: tags.append("⭐+80%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r
        if r["all_positive"] and r["avg_annual_ret"] >= 100:
            if best_both is None or r["avg_annual_ret"] > best_both[1]["avg_annual_ret"]:
                best_both = (name, r)
        if r["all_positive"]:
            if best_positive_only is None or r["avg_annual_ret"] > best_positive_only[1]["avg_annual_ret"]:
                best_positive_only = (name, r)

    print(f"\n{'=' * 145}")
    if best_both:
        r = best_both[1]
        print(f"🎉🎉🎉 **理想達成**: {best_both[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}%  / 5年計 {r['total_ret']:+.1f}%  / DD {r['max_dd']:.1f}%")
        print(f"   年別: {dict(r['yearly'])}")
    elif best_positive_only:
        r = best_positive_only[1]
        print(f"🎯 **毎年プラス達成 最高記録**: {best_positive_only[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 5年計 {r['total_ret']:+.1f}% / DD {r['max_dd']:.1f}%")
        print(f"   理想の +100% まで あと {100 - r['avg_annual_ret']:.1f}pp")
    print(f"{'=' * 145}")

    out = (Path(__file__).resolve().parent / "results" / "final_push.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
