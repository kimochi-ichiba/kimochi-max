"""新規上場スナイパーのバックテスト (v3.1③).

各銘柄の listing_date (キャッシュ内の最初の登場日) を「新規上場」とみなし、
上場後 N 日内に小額エントリー → TP (5x) または SL (-50%) で決済。

メイン戦略 (run_bt_v24) とは別管理で、最後に equity を合算する。

注意: 実際の memecoin (Pump.fun 等) より遥かに保守的な想定。
キャッシュには既に Binance/MEXC で生き残った銘柄のみが含まれているので、
「鳴かず飛ばず」率は実際より低く見積もられる (survivorship bias)。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


@dataclass
class SniperResult:
    final: float
    total_ret: float
    n_trades: int
    n_tp: int           # take profit 達成
    n_sl: int           # stop loss
    n_timeout: int      # 期限切れ exit
    win_rate: float
    avg_multiple: float


def simulate_sniper(
    all_data: dict[str, pd.DataFrame],
    universe: list[str],
    start: str,
    end: str,
    *,
    initial: float = 10_000.0,
    listing_days: int = 30,
    tp_multiple: float = 5.0,
    sl_pct: float = 0.50,
    alloc_per_trade_pct: float = 0.05,
    max_concurrent: int = 5,
    timeout_days: int = 180,
    fee: float = 0.0010,
    slip: float = 0.0050,        # 新規上場は scalp slippage 大きい
) -> SniperResult:
    """新規上場スナイパー戦略のシミュレーション.

    - 各銘柄の cache 内 earliest_ts を listing_date とする
    - 期間内の listing から listing_days 内に等しく分散エントリー
    - 同時保有 max_concurrent 銘柄まで
    - TP (tp_multiple = 5x で +400%) または SL (-sl_pct で -50%) で決済
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    # listing_date を抽出 (BTC 等の主流コインも含めて全銘柄)
    listings: list[tuple[pd.Timestamp, str]] = []
    for sym in universe:
        df = all_data.get(sym)
        if df is None or df.empty:
            continue
        first_ts = df.index.min()
        if start_ts <= first_ts <= end_ts:
            listings.append((first_ts, sym))
    listings.sort()

    if not listings:
        return SniperResult(initial, 0.0, 0, 0, 0, 0, 0.0, 0.0)

    cash = initial
    open_positions: dict[str, dict] = {}  # sym → {entry_price, entry_ts, qty}
    n_tp = n_sl = n_timeout = 0
    multiples: list[float] = []

    # 全期間の日付シーケンス (BTC ベース)
    btc_df = all_data.get("BTC/USDT")
    if btc_df is None:
        return SniperResult(initial, 0.0, 0, 0, 0, 0, 0.0, 0.0)
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    listing_idx = 0
    for date in dates:
        # 1) 新規上場銘柄をエントリー (listing 後 listing_days 内)
        while (listing_idx < len(listings)
               and listings[listing_idx][0] <= date):
            l_ts, sym = listings[listing_idx]
            days_since = (date - l_ts).days
            listing_idx += 1
            if days_since > listing_days:
                continue  # 上場から日が経ちすぎ
            if sym in open_positions:
                continue
            if len(open_positions) >= max_concurrent:
                continue
            df = all_data.get(sym)
            if df is None or date not in df.index:
                continue
            price = float(df.loc[date, "close"])
            buy_price = price * (1 + slip)
            alloc = initial * alloc_per_trade_pct
            if cash < alloc:
                continue
            qty = alloc / buy_price * (1 - fee)
            open_positions[sym] = {
                "entry_price": buy_price,
                "entry_ts": date,
                "qty": qty,
                "alloc": alloc,
            }
            cash -= alloc

        # 2) オープンポジションを毎日チェック (TP/SL/timeout)
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df = all_data.get(sym)
            if df is None or date not in df.index:
                continue
            high = float(df.loc[date, "high"])
            low = float(df.loc[date, "low"])
            entry = pos["entry_price"]

            tp_target = entry * tp_multiple
            sl_target = entry * (1 - sl_pct)

            exit_price = None
            reason = None
            # 同日中に SL も TP も触れる場合は SL 優先 (保守的)
            if low <= sl_target:
                exit_price = sl_target * (1 - slip)
                reason = "SL"
            elif high >= tp_target:
                exit_price = tp_target * (1 - slip)
                reason = "TP"
            elif (date - pos["entry_ts"]).days >= timeout_days:
                # timeout: 当日終値で exit
                exit_price = float(df.loc[date, "close"]) * (1 - slip)
                reason = "TIMEOUT"

            if exit_price is not None:
                proceeds = pos["qty"] * exit_price * (1 - fee)
                cash += proceeds
                multiple = proceeds / pos["alloc"]
                multiples.append(multiple)
                if reason == "TP":
                    n_tp += 1
                elif reason == "SL":
                    n_sl += 1
                else:
                    n_timeout += 1
                del open_positions[sym]

    # 期間終了時、未決済ポジションを timeout 扱いで決済
    last_date = dates[-1] if dates else None
    if last_date is not None:
        for sym, pos in list(open_positions.items()):
            df = all_data.get(sym)
            if df is None or last_date not in df.index:
                # 当該日付なしなら直前の close を使う
                close = float(df["close"].iloc[-1]) if df is not None else 0
            else:
                close = float(df.loc[last_date, "close"])
            proceeds = pos["qty"] * close * (1 - slip) * (1 - fee)
            cash += proceeds
            multiple = proceeds / pos["alloc"]
            multiples.append(multiple)
            n_timeout += 1
            del open_positions[sym]

    final = cash
    total_ret = (final - initial) / initial * 100
    n_trades = n_tp + n_sl + n_timeout
    win_rate = n_tp / n_trades * 100 if n_trades else 0.0
    avg_multiple = sum(multiples) / len(multiples) if multiples else 0.0

    return SniperResult(
        final=round(final, 2),
        total_ret=round(total_ret, 2),
        n_trades=n_trades, n_tp=n_tp, n_sl=n_sl, n_timeout=n_timeout,
        win_rate=round(win_rate, 1),
        avg_multiple=round(avg_multiple, 2),
    )


if __name__ == "__main__":
    # smoke test
    from wf_validate_v24 import load_cache, make_universe
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"ユニバース: {len(universe)}")
    for label, start, end in [
        ("2020-2022 ベア込み", "2020-01-01", "2022-12-31"),
        ("2023 回復", "2023-01-01", "2023-12-31"),
        ("2024 ブル", "2024-01-01", "2024-12-31"),
        ("2025 Q1", "2025-01-01", "2025-04-19"),
    ]:
        r = simulate_sniper(all_data, universe, start, end,
                              initial=10000)
        print(f"\n{label} [{start}〜{end}]:")
        print(f"  final ${r.final:,.0f} / total_ret {r.total_ret:+.1f}%")
        print(f"  trades {r.n_trades} (TP={r.n_tp}, SL={r.n_sl}, "
              f"timeout={r.n_timeout}) / win_rate {r.win_rate}%")
        print(f"  avg multiple: {r.avg_multiple}x")
