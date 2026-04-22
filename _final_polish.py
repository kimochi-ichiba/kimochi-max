"""2022年プラス化の最終反復"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ruin_proof_v2 import run_v2
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

    # B05 base (ベスト)
    base = dict(
        risk_per_trade_pct=0.02, max_pos=5,
        stop_loss_pct=0.05,
        partial_tp_pct=0.05, partial_tp_fraction=0.5,
        trail_activate_pct=0.15, trail_giveback_pct=0.04,
        adx_min=55, adx_lev2=60, adx_lev3=65,
        lev_low=2.0, lev_mid=3.0, lev_high=4.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=70,
        enable_short=True, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=30,
    )

    configs = [
        ("F01 B05原型(参考)",                    {**base}),
        ("F02 SHORT完全無効（LONG onlyベア休止）",   {**base, "enable_short": False}),
        ("F03 SHORT BTC ADX≥40超厳格",           {**base, "btc_adx_for_short": 40}),
        ("F04 SHORT BTC ADX≥45超々厳格",          {**base, "btc_adx_for_short": 45}),
        ("F05 SL3% タイト化",                    {**base, "stop_loss_pct": 0.03}),
        ("F06 SHORT RSI≥80 厳格",               {**base, "rsi_short_min": 80}),
        ("F07 SHORT RSI≥85 超厳格",              {**base, "rsi_short_min": 85}),
        ("F08 複合: SHORT無効+SL3%",              {**base, "enable_short": False, "stop_loss_pct": 0.03}),
        ("F09 F02+3pos集中",                    {**base, "enable_short": False, "max_pos": 3}),
        ("F10 F02+Lev3-5",                     {**base, "enable_short": False, "lev_low": 3.0, "lev_mid": 4.0, "lev_high": 5.0}),
        ("F11 F02+ADX50緩和",                   {**base, "enable_short": False, "adx_min": 50}),
        ("F12 F02+ADX60超厳選",                 {**base, "enable_short": False, "adx_min": 60}),
    ]

    print(f"{'=' * 155}")
    print(f"🎯 2022年プラス化 最終反復（毎年+勝率50%+年率高い 三冠目標）")
    print(f"{'=' * 155}")
    print(f"{'戦略':40s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'L/S':>9s} | 判定")
    print("-" * 155)

    results = {}
    best = None
    for name, cfg in configs:
        r = run_v2(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:40s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {r['n_long']:>3d}/{r['n_short']:>3d} | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["win_rate"] >= 50: tags.append("📈勝率50+")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r

        # 三冠判定
        if (r["all_positive"] and r["win_rate"] >= 50
            and r["avg_annual_ret"] >= 50):
            score = r["avg_annual_ret"] + r["win_rate"] - r["max_dd"]
            if best is None or score > best[2]:
                best = (name, r, score)

    print(f"\n{'=' * 155}")
    if best:
        r = best[1]
        print(f"🏆 三冠達成ベスト: {best[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}%  / 勝率 {r['win_rate']:.1f}%  / DD {r['max_dd']:.1f}%")
        print(f"   毎年プラス ✅  / 勝率50%+ ✅  / 年率50%+ ✅")
    else:
        yearly_plus = [(n, r) for n, r in results.items() if r["all_positive"]]
        if yearly_plus:
            print(f"🎯 毎年プラス達成:")
            for n, r in sorted(yearly_plus, key=lambda x: -x[1]["avg_annual_ret"]):
                print(f"   {n}: 年率{r['avg_annual_ret']:+.1f}% / 勝率{r['win_rate']:.1f}%")
        else:
            print(f"⚠️ 毎年プラス未達成")

    out = (Path(__file__).resolve().parent / "results" / "final_polish.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
