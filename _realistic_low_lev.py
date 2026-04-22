"""低レバ・広SL で現実派バックテストを反復"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _realistic_backtest import run_realistic
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
        risk_per_trade_pct=0.02, max_pos=10,
        stop_loss_pct=0.15,  # 広いSL
        tp1_pct=0.10, tp1_fraction=0.4,
        tp2_pct=0.25, tp2_fraction=0.5,
        trail_activate_pct=0.30, trail_giveback_pct=0.08,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.0, lev_mid=1.5, lev_high=2.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
    )

    configs = [
        ("L01 Lev1-2 SL15% 広",                         {**base}),
        ("L02 Lev1 固定 SL15%",                          {**base, "lev_low": 1.0, "lev_mid": 1.0, "lev_high": 1.0}),
        ("L03 Lev1-2 SL10%",                           {**base, "stop_loss_pct": 0.10}),
        ("L04 Lev1-2 SL20% 超広",                       {**base, "stop_loss_pct": 0.20}),
        ("L05 Lev1-2 max 20",                          {**base, "max_pos": 20}),
        ("L06 Lev1-2 max 5 集中",                       {**base, "max_pos": 5}),
        ("L07 Lev2固定 SL15%",                          {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0}),
        ("L08 Lev1-2 ADX60厳選",                        {**base, "adx_min": 60, "adx_lev2": 65, "adx_lev3": 75}),
        ("L09 Lev1-2 SL15% max10 リスク3%",              {**base, "risk_per_trade_pct": 0.03}),
        ("L10 Lev1-3 SL15%",                           {**base, "lev_high": 3.0}),
        ("L11 Lev1 固定 max 20",                        {**base, "lev_low": 1.0, "lev_mid": 1.0, "lev_high": 1.0, "max_pos": 20}),
        ("L12 Lev1.5 固定 max 15",                      {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5, "max_pos": 15}),
    ]

    print(f"{'=' * 170}")
    print(f"🎯 低レバ・広SL で現実派+100%探索")
    print(f"{'=' * 170}")
    print(f"{'戦略':40s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'清算':>4s} | 判定")
    print("-" * 170)

    results = {}
    best = None
    for name, cfg in configs:
        r = run_realistic(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:40s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {r['n_liquidations']:>3d} | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        elif r["no_negative"]: tags.append("🟢マイナス無")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        elif r["avg_annual_ret"] >= 20: tags.append("💪+20%")
        if r["win_rate"] >= 50: tags.append("📈勝率50+")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        if r["n_liquidations"] == 0: tags.append("✅清算0")
        row += " ".join(tags)
        print(row)
        results[name] = r
        if r["no_negative"] and r["integrity_ok"]:
            if best is None or r["avg_annual_ret"] > best[1]["avg_annual_ret"]:
                best = (name, r)

    print(f"\n{'=' * 170}")
    if best:
        r = best[1]
        print(f"🏆 現実派ベスト（マイナスなし）: {best[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / DD {r['max_dd']:.1f}% / 清算{r['n_liquidations']}回")
    else:
        all_sorted = sorted(results.items(), key=lambda x: -x[1]["avg_annual_ret"])
        print(f"🎯 年率TOP3:")
        for n, r in all_sorted[:3]:
            print(f"   {n}: 年率{r['avg_annual_ret']:+.1f}% / 清算{r['n_liquidations']}回 / DD{r['max_dd']:.1f}%")
    print(f"{'=' * 170}")

    out = (Path(__file__).resolve().parent / "results" / "realistic_low_lev.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
