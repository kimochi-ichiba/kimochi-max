"""
有名トレーダー手法 × 40反復バックテスト
=================================================
タートル、リバモア、ミネルビニ、シュワルツ、Seykota、O'Neilを組み合わせ
年率+50-70%に到達した時点で終了
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi
from _legends_engine import run_legends

TARGET_ANNUAL = 50.0  # この年率に達したら終了
CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")


def load_data():
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    if CACHE_PATH.exists():
        age_h = (time.time() - CACHE_PATH.stat().st_mtime) / 3600
        if age_h < 6:
            print(f"📦 キャッシュ使用（{age_h:.1f}h前）")
            with open(CACHE_PATH, "rb") as f:
                return pickle.load(f)
    print("📥 Binance実データ取得中...")
    t0 = time.time()
    d = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(d)}銘柄 ({time.time()-t0:.0f}秒)")
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(d, f)
    return d


def build_configs():
    """40パターンの戦略を構築。Phase毎にテーマを変える"""
    # 共通ベース (M07寄り)
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
        max_margin_per_pos_pct=0.10,
    )

    configs = []

    # ━━━ Phase 1: レバレッジ段階的強化 (I01-I08) ━━━
    configs.append(("I01 M07基準 Lev1 max20",
                    {**base}))
    configs.append(("I02 Lev1.5 max20 リスク2.5%",
                    {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5,
                     "risk_per_trade_pct": 0.025}))
    configs.append(("I03 Lev2 固定 max20",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0}))
    configs.append(("I04 Lev2 max20 リスク3% SL20%",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "risk_per_trade_pct": 0.03, "stop_loss_pct": 0.20}))
    configs.append(("I05 Lev2.5 max15 SL20%",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "max_pos": 15, "stop_loss_pct": 0.20}))
    configs.append(("I06 Lev3 max15 SL20% リスク2.5%",
                    {**base, "lev_low": 3.0, "lev_mid": 3.0, "lev_high": 3.0,
                     "max_pos": 15, "stop_loss_pct": 0.20, "risk_per_trade_pct": 0.025}))
    configs.append(("I07 Lev1-2動的 max20",
                    {**base, "lev_low": 1.0, "lev_mid": 1.5, "lev_high": 2.0}))
    configs.append(("I08 Lev2-3動的 max15 SL18%",
                    {**base, "lev_low": 2.0, "lev_mid": 2.5, "lev_high": 3.0,
                     "max_pos": 15, "stop_loss_pct": 0.18}))

    # ━━━ Phase 2: リバモア流ピラミディング (I09-I16) ━━━
    pyr = dict(pyramid_enabled=True, pyramid_max=2,
               pyramid_trigger_pct=0.10, pyramid_size_pct=0.5)
    configs.append(("I09 Lev1.5 ピラミ2回 max20",
                    {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5, **pyr}))
    configs.append(("I10 Lev2 ピラミ2回 SL20%",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, **pyr}))
    configs.append(("I11 Lev1.5 ピラミ3回 max15",
                    {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5,
                     "max_pos": 15, "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.08, "pyramid_size_pct": 0.4}))
    configs.append(("I12 Lev2 ピラミ2 トレ長め",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, "trail_activate_pct": 0.40,
                     "trail_giveback_pct": 0.10, **pyr}))
    configs.append(("I13 Lev2 ピラミ 大きめ(60%)",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, "pyramid_enabled": True, "pyramid_max": 2,
                     "pyramid_trigger_pct": 0.12, "pyramid_size_pct": 0.6}))
    configs.append(("I14 Lev1-2動 ピラミ max20",
                    {**base, "lev_low": 1.0, "lev_mid": 1.5, "lev_high": 2.0, **pyr}))
    configs.append(("I15 Lev2.5 ピラミ max10 SL22",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "max_pos": 10, "stop_loss_pct": 0.22, **pyr}))
    configs.append(("I16 Lev2 ピラミ SHORT有",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, "enable_short": True, **pyr}))

    # ━━━ Phase 3: タートル流 Donchian (I17-I24) ━━━
    dch = dict(donchian_enabled=True, donchian_n=20, donchian_exit_n=10)
    configs.append(("I17 Donch20/10 Lev1.5",
                    {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5,
                     "stop_loss_pct": 0.15, **dch}))
    configs.append(("I18 Donch20/10 Lev2",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, **dch}))
    configs.append(("I19 Donch55/20 Lev2 ADX40緩",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, "adx_min": 40, "adx_lev2": 50, "adx_lev3": 60,
                     "donchian_enabled": True, "donchian_n": 55, "donchian_exit_n": 20}))
    configs.append(("I20 Donch20 Lev2 ピラミ",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, **dch, **pyr}))
    configs.append(("I21 Donch20 Lev2.5 max15",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "max_pos": 15, "stop_loss_pct": 0.22, **dch}))
    configs.append(("I22 Donch20 Lev2 SHORT",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, "enable_short": True, **dch}))
    configs.append(("I23 Donch20 Lev1.5 ピラミ3",
                    {**base, "lev_low": 1.5, "lev_mid": 1.5, "lev_high": 1.5,
                     "pyramid_enabled": True, "pyramid_max": 3, "pyramid_trigger_pct": 0.10,
                     "pyramid_size_pct": 0.5, **dch}))
    configs.append(("I24 Donch20 Lev2 ピラミ SHORT",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, "enable_short": True, **dch, **pyr}))

    # ━━━ Phase 4: ミネルビニVCP + O'Neilボリューム (I25-I32) ━━━
    vcp = dict(vcp_enabled=True, vcp_atr_max_pct=0.06)
    vol = dict(volume_confirm=True, volume_mult=1.5)
    configs.append(("I25 VCP Lev2 max15",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "max_pos": 15, "stop_loss_pct": 0.15, **vcp}))
    configs.append(("I26 VCP+Vol Lev2 max15",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "max_pos": 15, "stop_loss_pct": 0.15, **vcp, **vol}))
    configs.append(("I27 VCP+Vol Lev2 ピラミ",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, **vcp, **vol, **pyr}))
    configs.append(("I28 VCP+Donch Lev2",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.18, **vcp, **dch}))
    configs.append(("I29 VCP+Donch+ピラミ Lev2",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, **vcp, **dch, **pyr}))
    configs.append(("I30 Vol+Donch Lev2.5 max10",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "max_pos": 10, "stop_loss_pct": 0.20, **vol, **dch}))
    configs.append(("I31 VCP+Vol Lev3 max10 厳格",
                    {**base, "lev_low": 3.0, "lev_mid": 3.0, "lev_high": 3.0,
                     "max_pos": 10, "stop_loss_pct": 0.18, "adx_min": 55, **vcp, **vol}))
    configs.append(("I32 VCP+Vol+Donch+ピラミ 全部",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, **vcp, **vol, **dch, **pyr}))

    # ━━━ Phase 5: 最終融合 (I33-I40) ━━━
    configs.append(("I33 Seykota Lev2 ピラミ3 TP遠",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20,
                     "tp1_pct": 0.15, "tp1_fraction": 0.3,
                     "tp2_pct": 0.40, "tp2_fraction": 0.4,
                     "trail_activate_pct": 0.50, "trail_giveback_pct": 0.12,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.12, "pyramid_size_pct": 0.4}))
    configs.append(("I34 Livermore完全 Lev2.5 ピラミ4",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "max_pos": 12, "stop_loss_pct": 0.22,
                     "tp1_pct": 0.10, "tp1_fraction": 0.25,
                     "tp2_pct": 0.30, "tp2_fraction": 0.35,
                     "trail_activate_pct": 0.50, "trail_giveback_pct": 0.15,
                     "pyramid_enabled": True, "pyramid_max": 4,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5}))
    configs.append(("I35 Turtle純 Donch55/20 Lev2",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, "adx_min": 40, "adx_lev2": 50, "adx_lev3": 60,
                     "donchian_enabled": True, "donchian_n": 55, "donchian_exit_n": 20,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.08, "pyramid_size_pct": 0.4}))
    configs.append(("I36 融合 Lev2 VCP+Donch+ピラミ3+SHORT",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "stop_loss_pct": 0.20, "enable_short": True,
                     **vcp, **dch,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.4,
                     "tp1_pct": 0.08, "tp2_pct": 0.20, "trail_activate_pct": 0.40}))
    configs.append(("I37 完全攻め Lev2.5 ピラミ3 max20",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "risk_per_trade_pct": 0.025, "stop_loss_pct": 0.22,
                     "tp1_pct": 0.10, "tp2_pct": 0.30,
                     "trail_activate_pct": 0.45, "trail_giveback_pct": 0.12,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.4}))
    configs.append(("I38 VCP+ピラミ Lev2 リスク3%",
                    {**base, "lev_low": 2.0, "lev_mid": 2.0, "lev_high": 2.0,
                     "risk_per_trade_pct": 0.03, "stop_loss_pct": 0.20,
                     **vcp, "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5}))
    configs.append(("I39 マルチ手法 Lev2.5 ADX45緩",
                    {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
                     "risk_per_trade_pct": 0.025, "stop_loss_pct": 0.22,
                     "adx_min": 45, "adx_lev2": 55, "adx_lev3": 65,
                     **vcp, **dch,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5,
                     "tp1_pct": 0.08, "tp2_pct": 0.25, "trail_activate_pct": 0.40}))
    configs.append(("I40 最終 Lev3 ピラミ3 全フィルタ",
                    {**base, "lev_low": 3.0, "lev_mid": 3.0, "lev_high": 3.0,
                     "max_pos": 12, "risk_per_trade_pct": 0.025, "stop_loss_pct": 0.22,
                     **vcp, **vol, **dch,
                     "pyramid_enabled": True, "pyramid_max": 3,
                     "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.4,
                     "tp1_pct": 0.10, "tp2_pct": 0.30, "trail_activate_pct": 0.45}))

    return configs


def tag_row(r):
    tags = []
    if r["all_positive"]: tags.append("🎯毎年+")
    elif r["no_negative"]: tags.append("🟢ﾏｲﾅｽ無")
    if r["avg_annual_ret"] >= 70: tags.append("🚀+70%")
    elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
    elif r["avg_annual_ret"] >= 30: tags.append("💪+30%")
    if r["win_rate"] >= 55: tags.append("📈勝率55+")
    if r["max_dd"] < 40: tags.append("🛡DD<40")
    if r["n_liquidations"] == 0: tags.append("✅清算0")
    return " ".join(tags)


def main():
    all_data = load_data()
    configs = build_configs()
    print(f"\n{'=' * 170}")
    print(f"🎯 年利+{TARGET_ANNUAL:.0f}%達成まで最大40反復 (伝説トレーダー手法統合)")
    print(f"{'=' * 170}")
    print(f"{'No':3s} | {'戦略':42s} | {'20':>5s} {'21':>5s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'DD':>5s} | {'勝率':>5s} | {'取引':>5s} | {'清算':>4s} | 判定")
    print("-" * 170)

    results = {}
    best = None
    early_exit = False

    for idx, (name, cfg) in enumerate(configs, 1):
        t0 = time.time()
        r = run_legends(all_data, "2020-01-01", "2024-12-31", cfg)
        elapsed = time.time() - t0
        row = f"{idx:3d} | {name:42s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+4.1f}% "
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['win_rate']:>4.1f}% | {r['n_trades']:>4d} | {r['n_liquidations']:>3d} | "
        row += tag_row(r)
        print(row, flush=True)
        results[name] = r

        if r["integrity_ok"]:
            if r["no_negative"]:
                if best is None or r["avg_annual_ret"] > best[1]["avg_annual_ret"]:
                    best = (name, r)
            if r["avg_annual_ret"] >= TARGET_ANNUAL and r["no_negative"]:
                print(f"\n🎉 目標達成！ {name} 年率 {r['avg_annual_ret']:+.1f}%")
                early_exit = True
                break

    print(f"\n{'=' * 170}")
    if early_exit:
        print(f"✅ 早期終了（目標達成）")
    else:
        print(f"⚠️  {len(results)}/40 実行完了 — 目標未達")
    if best:
        n, r = best
        print(f"🏆 ベスト: {n}")
        print(f"   年率 {r['avg_annual_ret']:+.1f}% / 勝率 {r['win_rate']:.1f}% / "
              f"DD {r['max_dd']:.1f}% / 清算{r['n_liquidations']}回 / 取引{r['n_trades']} / "
              f"$10K→${r['final']:,.0f}")
    else:
        # マイナス年ありでも最高を表示
        valid = [(n, r) for n, r in results.items() if r["integrity_ok"]]
        if valid:
            valid.sort(key=lambda x: -x[1]["avg_annual_ret"])
            n, r = valid[0]
            print(f"🏅 マイナス年あり TOP: {n}")
            print(f"   年率 {r['avg_annual_ret']:+.1f}% / DD {r['max_dd']:.1f}% / "
                  f"$10K→${r['final']:,.0f} (負年{r['negative_years']})")
    print(f"{'=' * 170}")

    out = (Path(__file__).resolve().parent / "results" / "iterate_40.json")
    out.write_text(json.dumps({
        "results": results,
        "best": best[0] if best else None,
        "early_exit": early_exit,
        "target_reached": early_exit,
    }, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")


if __name__ == "__main__":
    main()
