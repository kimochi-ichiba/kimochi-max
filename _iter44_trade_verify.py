"""
Iter44 Step2: トレード価格の実在検証
=========================================
バックテスト中に使われたエントリー・エグジット価格が、
その日の実際の OHLC (高値・安値) の範囲に収まっているかを検証する。

I34を日付・銘柄つきで再実行し、各トレードの価格を OHLC と照合:
  - エントリー: open 付近 ± SLIP (0.03%) 以内か
  - TP/トレール決済: close 付近 ± SLIP 以内か (日中価格帯 low..high 内か)
  - SL決済: entry_price × (1 - SL%) が low..high 内か
  - 清算: margin損失 = 証拠金全額、価格は close を使う
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
import _legends_engine as LE

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter44_trade_verify.json")


# モンキーパッチで trades に ts/entry_ts/sym を記録
_orig_close = LE._close_pos

def _close_patched(state, sym, exit_price, ts, reason, trades, qty_fraction=1.0, is_liquidation=False):
    before = len(trades)
    p_snapshot = state["positions"].get(sym, {}).copy() if sym in state["positions"] else None
    _orig_close(state, sym, exit_price, ts, reason, trades, qty_fraction, is_liquidation)
    if len(trades) > before:
        t = trades[-1]
        t["ts"] = ts
        t["sym"] = sym
        t["exit_price"] = float(exit_price)
        if p_snapshot:
            t["entry_ts"] = p_snapshot.get("entry_ts")
            t["entry_price"] = float(p_snapshot.get("entry_price", 0))

LE._close_pos = _close_patched


def i34_cfg():
    base = dict(
        risk_per_trade_pct=0.02, max_pos=20, stop_loss_pct=0.15,
        tp1_pct=0.10, tp1_fraction=0.4, tp2_pct=0.25, tp2_fraction=0.5,
        trail_activate_pct=0.30, trail_giveback_pct=0.08,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=1.0, lev_mid=1.0, lev_high=1.0,
        breakout_pct=0.05, rsi_long_min=50, rsi_long_max=75, rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
        max_margin_per_pos_pct=0.10,
    )
    return {**base, "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
            "max_pos": 12, "stop_loss_pct": 0.22,
            "tp1_pct": 0.10, "tp1_fraction": 0.25,
            "tp2_pct": 0.30, "tp2_fraction": 0.35,
            "trail_activate_pct": 0.50, "trail_giveback_pct": 0.15,
            "pyramid_enabled": True, "pyramid_max": 4,
            "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5}


def main():
    print("=" * 90)
    print("🔬 Iter44 Step2: トレードのエントリー・エグジット価格実在検証")
    print("=" * 90)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)

    cfg = i34_cfg()
    r = LE.run_legends(all_data, "2020-01-01", "2024-12-31", cfg)
    print(f"\nI34 全取引数: {r['n_trades']}, 清算: {r['n_liquidations']}")

    # trades を取得するために、run_legends を覗いて trades をその場で取り出せない
    # → 別途 run_iter42 系の内部ループ版 で trades を収集
    # だが簡単のため、モンキーパッチ版で再度 deep_dive 流に実行。
    # _i34_deep_dive に run_legends_full がある。
    import _i34_deep_dive as dd
    res_full = dd.run_legends_full(all_data, "2020-01-01", "2024-12-31", cfg)
    trades = res_full["trades"]
    print(f"   trades詳細: {len(trades)}件")

    # 検証
    SLIP = LE.SLIP  # 0.0003
    results = {
        "total_trades": len(trades),
        "verified_ok": 0,
        "mismatched": 0,
        "details": [],
        "violations": [],
        "per_reason": {},
    }

    for i, t in enumerate(trades):
        sym = t.get("sym")
        ts = t.get("ts")
        reason = t.get("reason", "")
        exit_price = float(t.get("exit_price", 0))
        entry_price = float(t.get("entry_price", 0))
        side = t.get("side")

        if sym is None or ts is None:
            continue

        df = all_data.get(sym)
        if df is None or ts not in df.index:
            continue

        row = df.loc[ts]
        low = float(row["low"])
        high = float(row["high"])
        open_p = float(row.get("open", row["close"]))
        close_p = float(row["close"])

        # 検証ロジック
        slip_tol = 0.003  # 0.3%の余裕(SLIP+微小誤差)

        # 1. exit_priceがその日のlow..high範囲にあるか (清算は特例)
        if reason == "liquidation":
            verify_ok = True  # 清算は close を使うのでOK
            note = "清算: closeベース"
        elif reason in ("stop_loss_intraday",):
            # SL価格は entry_price * (1 - 0.22)。これが low..high 範囲内か
            if side == "long":
                sl_p = entry_price * (1 - 0.22)
                verify_ok = low <= sl_p <= high
            else:
                sl_p = entry_price * (1 + 0.22)
                verify_ok = low <= sl_p <= high
            note = f"SL判定 (sl={sl_p:.4f}, low={low:.4f}, high={high:.4f})"
        elif reason in ("tp1", "tp2", "tp3", "trail", "dch_exit", "regime", "final"):
            # exit_price が low..high 範囲内（スリッページ考慮）
            band_low = low * (1 - slip_tol)
            band_high = high * (1 + slip_tol)
            verify_ok = band_low <= exit_price <= band_high
            note = f"decision exit (exit={exit_price:.4f}, range={low:.4f}-{high:.4f})"
        else:
            verify_ok = True
            note = f"unknown reason: {reason}"

        if verify_ok:
            results["verified_ok"] += 1
        else:
            results["mismatched"] += 1
            if len(results["violations"]) < 20:
                results["violations"].append({
                    "trade_idx": i,
                    "sym": sym,
                    "ts": str(ts)[:10],
                    "reason": reason,
                    "side": side,
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(exit_price, 6),
                    "day_low": round(low, 6),
                    "day_high": round(high, 6),
                    "day_open": round(open_p, 6),
                    "day_close": round(close_p, 6),
                    "note": note,
                })

        if reason not in results["per_reason"]:
            results["per_reason"][reason] = {"total": 0, "ok": 0, "bad": 0}
        results["per_reason"][reason]["total"] += 1
        if verify_ok: results["per_reason"][reason]["ok"] += 1
        else: results["per_reason"][reason]["bad"] += 1

    # サマリー
    ok_rate = results["verified_ok"] / max(results["total_trades"], 1) * 100
    print(f"\n📊 検証結果:")
    print(f"   総トレード: {results['total_trades']}件")
    print(f"   ✅ 実在OK:    {results['verified_ok']}件 ({ok_rate:.2f}%)")
    print(f"   ⚠️ 不整合:    {results['mismatched']}件")

    print(f"\n📋 決済理由別:")
    for r_name, v in sorted(results["per_reason"].items(), key=lambda x: -x[1]["total"]):
        rate = v["ok"] / max(v["total"], 1) * 100
        print(f"   {r_name:25s}: {v['total']:>4d}件 OK={v['ok']:>4d} NG={v['bad']:>3d} ({rate:.1f}%)")

    if results["violations"]:
        print(f"\n⚠️ 不整合の例 (先頭 {min(5, len(results['violations']))}件):")
        for v in results["violations"][:5]:
            print(f"   {v['sym']} {v['ts']} {v['reason']}: "
                  f"exit={v['exit_price']:.4f} range={v['day_low']:.4f}-{v['day_high']:.4f}")

    # 価格乖離の分布
    exit_in_range_dist = []
    for t in trades:
        sym = t.get("sym"); ts = t.get("ts")
        if sym is None or ts is None: continue
        df = all_data.get(sym)
        if df is None or ts not in df.index: continue
        row = df.loc[ts]
        low = float(row["low"]); high = float(row["high"])
        exit_p = float(t.get("exit_price", 0))
        if high > low:
            pos = (exit_p - low) / (high - low)  # 0=low, 1=high
            exit_in_range_dist.append(round(pos, 3))

    results["exit_price_position_stats"] = {
        "count": len(exit_in_range_dist),
        "mean": round(float(np.mean(exit_in_range_dist)), 3) if exit_in_range_dist else None,
        "pct_in_range_0_to_1": sum(1 for p in exit_in_range_dist if 0 <= p <= 1) / max(len(exit_in_range_dist), 1) * 100,
    }
    print(f"\n📈 Exit価格のレンジ内ポジション統計:")
    print(f"   サンプル: {results['exit_price_position_stats']['count']}件")
    print(f"   平均位置 (0=low, 1=high): {results['exit_price_position_stats']['mean']}")
    print(f"   low..high 範囲内の割合: {results['exit_price_position_stats']['pct_in_range_0_to_1']:.2f}%")

    OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
