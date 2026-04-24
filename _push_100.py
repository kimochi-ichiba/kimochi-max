"""年率+100%への最終挑戦: 2022年もプラスを狙う"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bulletproof_backtest import run
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

    # P05 base (最良)
    base = dict(
        risk_per_trade_pct=0.02, max_pos=8,
        stop_loss_pct=0.05,
        tp1_pct=0.05, tp1_fraction=0.4,
        tp2_pct=0.12, tp2_fraction=0.5,
        trail_activate_pct=0.20, trail_giveback_pct=0.05,
        adx_min=55, adx_lev2=60, adx_lev3=70,
        lev_low=3.0, lev_mid=4.0, lev_high=5.0,
        breakout_pct=0.05,
        rsi_long_min=50, rsi_long_max=75,
        rsi_short_min=85,  # 超厳格SHORT
        enable_short=True, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,  # 超厳格BTC条件
    )

    configs = [
        ("X01 P05+SHORT超厳格(RSI85,BTC ADX40)",       {**base}),
        ("X02 X01+Lev4-6",                           {**base, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0}),
        ("X03 X01+max_pos 10",                       {**base, "max_pos": 10}),
        ("X04 X01+max_pos 15",                       {**base, "max_pos": 15}),
        ("X05 X01+Lev4-6+max_pos 10",                {**base, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0, "max_pos": 10}),
        ("X06 X01+リスク3%+Lev4-6",                    {**base, "risk_per_trade_pct": 0.03, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0}),
        ("X07 X01+Lev5-7 超高レバ",                    {**base, "lev_low": 5.0, "lev_mid": 6.0, "lev_high": 7.0}),
        ("X08 X01+ADX50 エントリー増",                   {**base, "adx_min": 50, "adx_lev2": 55, "adx_lev3": 65}),
        ("X09 X01+利益ロックOFF",                     {**base, "year_profit_lock": False}),
        ("X10 X01+ブレイク3%+ADX50",                   {**base, "breakout_pct": 0.03, "adx_min": 50, "adx_lev2": 55, "adx_lev3": 65}),
        ("X11 X03 Lev4-6 リスク3%",
         {**base, "max_pos": 10, "lev_low": 4.0, "lev_mid": 5.0, "lev_high": 6.0, "risk_per_trade_pct": 0.03}),
        ("X12 完全攻撃 Lev5-8 max_pos 15",
         {**base, "max_pos": 15, "lev_low": 5.0, "lev_mid": 6.5, "lev_high": 8.0, "risk_per_trade_pct": 0.03}),
    ]

    print(f"{'=' * 170}")
    print(f"🎯 年率+100%への最終挑戦（バグ修正済み・整合性保証）")
    print(f"{'=' * 170}")
    print(f"{'戦略':40s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'整合':>4s} | 判定")
    print("-" * 170)

    results = {}
    best_triple = None  # マイナス無し + 年率+100% + 勝率50+
    for name, cfg in configs:
        r = run(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:40s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {'✅' if r['integrity_ok'] else '❌'}  | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        elif r["no_negative"]: tags.append("🟢マイナス無し")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 75: tags.append("⭐+75%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["win_rate"] >= 50: tags.append("📈勝率50+")
        if r["max_dd"] < 50: tags.append("🛡DD<50")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r
        if (r["no_negative"] and r["avg_annual_ret"] >= 100
            and r["win_rate"] >= 50 and r["integrity_ok"]):
            if best_triple is None or r["avg_annual_ret"] > best_triple[1]["avg_annual_ret"]:
                best_triple = (name, r)

    ok = sum(1 for r in results.values() if r["integrity_ok"])
    print(f"\n整合性: {ok}/{len(results)} OK ✅")

    print(f"\n{'=' * 170}")
    if best_triple:
        r = best_triple[1]
        print(f"🎉🎉🎉 **三冠達成**: {best_triple[0]}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / DD {r['max_dd']:.1f}%")
        print(f"   $10K → 5年後 ${r['final']:,.0f}")
    else:
        # マイナスなし TOP
        no_neg = [(n, r) for n, r in results.items() if r["no_negative"] and r["integrity_ok"]]
        no_neg.sort(key=lambda x: -x[1]["avg_annual_ret"])
        if no_neg:
            print(f"🎯 マイナス無し TOP3:")
            for n, r in no_neg[:3]:
                print(f"   {n}: 年率{r['avg_annual_ret']:+.1f}% / 勝率{r['win_rate']:.1f}% / DD{r['max_dd']:.1f}% / $10K→${r['final']:,.0f}")
    print(f"{'=' * 170}")

    out = (Path(__file__).resolve().parent / "results" / "push_100.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
