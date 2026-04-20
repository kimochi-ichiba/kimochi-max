"""最終あと7pp: R39ベースで+100%目指す"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
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

    # R39 = ブレイクアウト3%, Lev3-4, ADX45, 5pos, リスク2%, SL4%, SHORTあり, 利益ロック
    base = dict(
        risk_per_trade_pct=0.02, max_pos=5,
        stop_loss_pct=0.04, trail_activate_pct=0.15, trail_giveback_pct=0.04,
        adx_min=45, adx_lev2=50, adx_lev3=60,
        lev_low=3.0, lev_mid=3.5, lev_high=4.0,
        breakout_pct_above_ema200=0.03, enable_short=True,
        year_profit_lock=True, btc_adx_for_short=30,
    )

    configs = [
        ("R39 原型 (年率+93.0% / 毎年+)",                        {**base}),
        ("R50 Lev3-5 (強化)",                                  {**base, "lev_high": 5.0, "lev_mid": 4.0}),
        ("R51 ブレイクアウト2% エントリー頻度up",                     {**base, "breakout_pct_above_ema200": 0.02}),
        ("R52 max_pos 7",                                     {**base, "max_pos": 7}),
        ("R53 リスク2.5%",                                      {**base, "risk_per_trade_pct": 0.025}),
        ("R54 トレール+20%",                                    {**base, "trail_activate_pct": 0.20}),
        ("R55 SL5% + Lev3-5",                                {**base, "stop_loss_pct": 0.05, "lev_high": 5.0, "lev_mid": 4.0}),
        ("R56 複合: Lev3-5 + ブレイク2% + max_pos 7",
         {**base, "lev_high": 5.0, "lev_mid": 4.0, "breakout_pct_above_ema200": 0.02, "max_pos": 7}),
        ("R57 ADX40 + Lev3-4 + ブレイク3%",
         {**base, "adx_min": 40, "adx_lev2": 48, "adx_lev3": 56}),
        ("R58 利益ロックOFF + R39",                              {**base, "year_profit_lock": False}),
        ("R59 SHORT無効 + R39",                               {**base, "enable_short": False}),
        ("R60 リスク3% + Lev3-5 + 7pos",
         {**base, "risk_per_trade_pct": 0.03, "lev_high": 5.0, "lev_mid": 4.0, "max_pos": 7}),
    ]

    print(f"{'=' * 145}")
    print(f"🎯 最終攻勢: +100%/年 達成へ")
    print(f"{'=' * 145}")
    print(f"{'戦略':48s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>7s} | {'DD':>5s} | 判定")
    print("-" * 145)

    results = {}
    winners = []
    for name, cfg in configs:
        r = run_ruin_proof(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:48s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 90: tags.append("⭐+90%")
        elif r["avg_annual_ret"] >= 70: tags.append("⭐+70%")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r
        if r["all_positive"]:
            winners.append((name, r))

    print(f"\n{'=' * 145}")
    winners.sort(key=lambda x: -x[1]["avg_annual_ret"])
    if winners and winners[0][1]["avg_annual_ret"] >= 100:
        r = winners[0][1]
        print(f"🎉🎉🎉 **理想達成**: {winners[0][0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}%  / 5年 {r['total_ret']:+.1f}%  / DD {r['max_dd']:.1f}%")
    elif winners:
        print(f"🎯 毎年プラス達成 TOP3:")
        for n, r in winners[:3]:
            print(f"   {n}: 年率{r['avg_annual_ret']:+.1f}% / DD{r['max_dd']:.1f}%")
    print(f"{'=' * 145}")

    out = Path("/Users/sanosano/projects/kimochi-max/results/last_push.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
