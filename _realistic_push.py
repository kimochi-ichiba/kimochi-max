"""現実派でも年率+30-50%を目指す最終反復"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
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

    # L11 をベースに最適化
    base = dict(
        risk_per_trade_pct=0.02, max_pos=20,
        stop_loss_pct=0.15,
        tp1_pct=0.10, tp1_fraction=0.4,
        tp2_pct=0.25, tp2_fraction=0.5,
        trail_activate_pct=0.30, trail_giveback_pct=0.08,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.0, lev_mid=1.0, lev_high=1.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
    )

    configs = [
        ("M01 L11基準 (Lev1固定 max20)",                  {**base}),
        ("M02 Lev2固定 max15",                           {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0, "max_pos": 15}),
        ("M03 Lev2固定 max20 SL20%",                     {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0, "stop_loss_pct": 0.20}),
        ("M04 Lev1固定 max30 (分散強化)",                   {**base, "max_pos": 30}),
        ("M05 Lev1-2 動的 max20",                         {**base, "lev_low": 1.0, "lev_mid": 1.5, "lev_high": 2.0}),
        ("M06 L11+SHORT有効(BTC ADX40)",                 {**base, "enable_short": True}),
        ("M07 Lev1 max20 TP低め(5/15)",
         {**base, "tp1_pct": 0.05, "tp2_pct": 0.15}),
        ("M08 Lev1 max20 トレール早め(15/5)",
         {**base, "trail_activate_pct": 0.15, "trail_giveback_pct": 0.05}),
        ("M09 Lev1 max20 ADX45緩和",                     {**base, "adx_min": 45, "adx_lev2": 55, "adx_lev3": 65}),
        ("M10 Lev1.5固定 max25",
         {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5, "max_pos": 25}),
        ("M11 Lev2 max30 SL15% リスク3%",
         {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0, "max_pos": 30, "risk_per_trade_pct": 0.03}),
        ("M12 Lev2 max20 トレール早め",
         {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0, "trail_activate_pct": 0.15, "trail_giveback_pct": 0.05}),
    ]

    print(f"{'=' * 170}")
    print(f"🎯 現実派で年率+30-50%目指す")
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
        if r["avg_annual_ret"] >= 50: tags.append("🚀+50%")
        elif r["avg_annual_ret"] >= 30: tags.append("⭐+30%")
        elif r["avg_annual_ret"] >= 20: tags.append("💪+20%")
        if r["win_rate"] >= 60: tags.append("📈勝率60+")
        if r["max_dd"] < 40: tags.append("🛡DD<40")
        if r["n_liquidations"] == 0: tags.append("✅清算0")
        row += " ".join(tags)
        print(row)
        results[name] = r
        if r["integrity_ok"]:
            if best is None or r["avg_annual_ret"] > best[1]["avg_annual_ret"]:
                best = (name, r)

    print(f"\n{'=' * 170}")
    if best:
        r = best[1]
        print(f"🏆 最高年率: {best[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / DD {r['max_dd']:.1f}% / 清算{r['n_liquidations']}回")
        print(f"   $10K → 5年後 ${r['final']:,.0f}")
    print(f"{'=' * 170}")

    out = Path("/Users/sanosano/projects/kimochi-max/results/realistic_push.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
