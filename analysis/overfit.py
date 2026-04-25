"""過学習検出指標.

参考: Bailey & López de Prado (2014), "The Probability of Backtest Overfitting"
       Bailey & López de Prado (2014), "The Deflated Sharpe Ratio"

実装:
- deflated_sharpe_ratio: N 試行の最大 Sharpe を期待最大値で deflate
- probability_of_backtest_overfitting (CSCV): IS top1 が OOS で 50% 以下に落ちる確率
- regime_consistency_score: min(bull,bear)/max(bull,bear) (C3 検出用)

使い方:
    from analysis.overfit import (
        deflated_sharpe_ratio, probability_of_backtest_overfitting,
        regime_consistency_score
    )

    dsr = deflated_sharpe_ratio(returns_series, n_trials=100)
    if dsr < 0.95: warning("過学習疑い")
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ─────────────────────────────
# Deflated Sharpe Ratio
# ─────────────────────────────
def deflated_sharpe_ratio(
    returns: pd.Series,
    n_trials: int,
    *,
    sr_benchmark: float = 0.0,
) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    SR_0 = E[max(SR_i)] for N i.i.d. trials を Euler-Mascheroni で近似。
    実 SR がそれを超える確率 (= cdf) を返す。

    Args:
        returns: 日次リターン (絶対値、0.01 = +1%)
        n_trials: 候補試行数 (grid search なら格子セル数)
        sr_benchmark: 比較対象の Sharpe (デフォルト 0)

    Returns:
        DSR ∈ [0, 1]、0.95 以上で「過学習でない」とよく判定される。
    """
    from scipy.stats import norm

    if len(returns) < 30:
        return 0.0

    sr = returns.mean() / returns.std() * np.sqrt(252)
    skew = returns.skew()
    kurt = returns.kurtosis()  # excess kurtosis (SciPy/pandas は -3 済み)
    T = len(returns)

    if n_trials < 2:
        n_trials = 2
    gamma = 0.5772
    log_n = np.log(n_trials)
    sr_0 = (np.sqrt(2 * log_n) - gamma / np.sqrt(2 * log_n)) + sr_benchmark

    # 観測 SR の分散 (López de Prado 式 14)
    var_sr = (1 - skew * sr + (kurt + 2) / 4 * sr ** 2) / (T - 1)
    if var_sr <= 0:
        return float("nan")

    z = (sr - sr_0) / np.sqrt(var_sr)
    return float(norm.cdf(z))


# ─────────────────────────────
# CSCV / PBO
# ─────────────────────────────
def probability_of_backtest_overfitting(
    returns_matrix: pd.DataFrame,
    *,
    n_partitions: int = 16,
) -> float:
    """CSCV (Combinatorially Symmetric Cross-Validation) で PBO 算出.

    Args:
        returns_matrix: shape (T, N)、各列が異なる戦略のリターン系列
        n_partitions: 分割数 S (偶数推奨、計算量は C(S, S/2))

    Returns:
        PBO ∈ [0, 1]、IS top1 が OOS 中央値以下に落ちる確率。
        0.5 が偶然レベル、低いほど良い。
    """
    if returns_matrix.shape[1] < 2:
        return 0.0

    if n_partitions % 2 != 0:
        n_partitions += 1
    if n_partitions > len(returns_matrix):
        n_partitions = max(2, len(returns_matrix) // 30 * 2)

    T, N = returns_matrix.shape
    chunk_size = T // n_partitions
    if chunk_size < 5:
        return 0.0

    # 等分割
    chunks = []
    for i in range(n_partitions):
        start = i * chunk_size
        end = start + chunk_size if i < n_partitions - 1 else T
        chunks.append(returns_matrix.iloc[start:end])

    from itertools import combinations

    # 全 C(S, S/2) 組み合わせで IS/OOS 分割
    half = n_partitions // 2
    n_logits_le_zero = 0
    n_total = 0

    for is_idx in combinations(range(n_partitions), half):
        oos_idx = [i for i in range(n_partitions) if i not in is_idx]

        is_chunks = [chunks[i] for i in is_idx]
        oos_chunks = [chunks[i] for i in oos_idx]
        is_concat = pd.concat(is_chunks, axis=0)
        oos_concat = pd.concat(oos_chunks, axis=0)

        # 各列 (戦略) の Sharpe を計算
        is_sharpe = is_concat.mean() / is_concat.std() * np.sqrt(252)
        oos_sharpe = oos_concat.mean() / oos_concat.std() * np.sqrt(252)

        if is_sharpe.dropna().empty or oos_sharpe.dropna().empty:
            continue

        # IS top1 の OOS rank (中央値より上か下か)
        best_is_strategy = is_sharpe.idxmax()
        if best_is_strategy not in oos_sharpe.index:
            continue

        oos_rank = oos_sharpe.rank(ascending=False)
        oos_relative_rank = oos_rank[best_is_strategy] / N  # 0=top, 1=bottom
        if oos_relative_rank > 0.5:
            n_logits_le_zero += 1
        n_total += 1

    if n_total == 0:
        return 0.0
    return n_logits_le_zero / n_total


# ─────────────────────────────
# regime_consistency
# ─────────────────────────────
def regime_consistency_score(
    bull_sharpe: float,
    bear_sharpe: float,
) -> float | None:
    """min/max ratio を返す。両方とも 0 なら None.

    判定:
        score >= 0.5: regime 跨ぎで安定
        0.2 <= score < 0.5: 軽度依存
        score < 0.2: 強い regime 依存 (C3 級、不採用推奨)
    """
    vals = [bull_sharpe, bear_sharpe]
    if max(map(abs, vals)) == 0:
        return None
    if max(vals) <= 0:
        # 両方ともマイナス: max(abs) で割って符号同じなら高 score
        ratio = min(vals) / max(vals)
        return float(ratio) if max(vals) != 0 else None
    return float(min(vals) / max(vals))


def regime_consistency_score_from_db(
    run_id: str,
    db_path: str = "data/kimochi.db",
) -> float | None:
    """run_id から DB を引いて regime_consistency_score を算出."""
    from db.connection import get_connection
    conn = get_connection(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT regime, sharpe FROM run_regimes "
            "WHERE run_id=? AND regime IN ('bull','bear')",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    s = {r["regime"]: r["sharpe"] for r in rows}
    if "bull" not in s or "bear" not in s:
        return None
    return regime_consistency_score(s["bull"], s["bear"])


# ─────────────────────────────
# bootstrap CI
# ─────────────────────────────
def bootstrap_sharpe_ci(
    returns: pd.Series,
    *,
    n_iter: int = 1000,
    confidence: float = 0.95,
    seed: int | None = 42,
) -> tuple[float, float]:
    """Sharpe の bootstrap 信頼区間 (low, high)."""
    if len(returns) < 30:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    arr = returns.to_numpy()
    sharpes = []
    n = len(arr)
    for _ in range(n_iter):
        sample = rng.choice(arr, size=n, replace=True)
        sd = sample.std()
        if sd > 0:
            sharpes.append(sample.mean() / sd * np.sqrt(252))
    sharpes = np.array(sharpes)
    alpha = (1 - confidence) / 2
    return (float(np.quantile(sharpes, alpha)), float(np.quantile(sharpes, 1 - alpha)))
