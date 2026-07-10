"""
Backtest harness — produces real MetricComparison samples for the
self-tuning evaluator from historical (regime, trade outcome) data.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 3.

For `hmm.entropy_threshold` / `hmm.entropy_scalar_floor`, no retraining is
required: RegimePrediction.position_scalar(cfg) is a pure function of a
stored entropy value and the HMM config, so champion vs. challenger can
be replayed against the same historical (entropy, raw_return) pairs
without re-running the detector or re-fitting anything. This keeps the
cheapest, lowest-risk parameters cheap to evaluate, matching the priority
order in the design doc's parameter table (§3).

Fold construction follows the same purge-gap principle as the live CPCV
split (FeatureSettings.cpcv_n_splits / purge_gap_bars) -- contiguous
blocks with a gap between them -- so adjacent folds aren't dominated by
the same short-term autocorrelated stretch of trades, without requiring
the full walk-forward train/test machinery (there is no "training" step
here, only a deterministic re-scaling of realized returns).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from src.config import FeatureSettings, HMMSettings
from src.tuning.evaluator import ChallengerEvaluator, MetricComparison


@dataclass(frozen=True)
class TradeSample:
    """One historical closed trade, enriched with the regime entropy at
    signal time. `raw_return` is the trade's realized return as a fraction
    of notional, computed at full (unscaled) position size."""

    entropy: float
    raw_return: float


class InsufficientDataError(ValueError):
    """Raised when there are too few trade samples to form the configured folds."""


def _position_scalar(entropy: float, threshold: float, floor: float) -> float:
    """Standalone reimplementation of RegimePrediction.position_scalar's
    math, operating on a stored entropy value rather than a live
    RegimePrediction object (avoids needing to reconstruct one)."""
    if entropy <= threshold:
        return 1.0
    span = 1.0 - threshold
    if span <= 0.0:
        return floor
    t = min(1.0, (entropy - threshold) / span)
    return 1.0 - t * (1.0 - floor)


def _make_folds(n: int, n_splits: int, purge_gap: int) -> list[tuple[int, int]]:
    """Contiguous index folds over [0, n) with a purge gap between them."""
    if n_splits < 1:
        raise ValueError(f"n_splits must be >= 1, got {n_splits}")
    fold_width = n // n_splits
    if fold_width <= purge_gap:
        raise InsufficientDataError(
            f"not enough samples ({n}) to form {n_splits} folds with "
            f"purge_gap={purge_gap} (need > {purge_gap} samples per fold)"
        )
    folds: list[tuple[int, int]] = []
    start = 0
    for i in range(n_splits):
        end = start + fold_width if i < n_splits - 1 else n
        usable_end = max(start, end - purge_gap) if i < n_splits - 1 else end
        folds.append((start, usable_end))
        start = end
    return folds


def _max_drawdown_inverted(returns: list[float]) -> float:
    """1 - max_drawdown_fraction over the cumulative return path within a
    fold. Higher is better, consistent with the "higher is better"
    convention ChallengerEvaluator expects."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_dd = max(max_dd, drawdown)
    return 1.0 - max_dd


def run_entropy_threshold_backtest(
    samples: list[TradeSample],
    champion_threshold: float,
    champion_floor: float,
    challenger_threshold: float,
    challenger_floor: float,
    features_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Replay historical trades under champion vs. challenger entropy-gate
    config, fold the results per FeatureSettings.cpcv_n_splits with a
    purge gap, and return significance-tested comparisons for
    oos_sharpe, win_rate, and max_drawdown_inverted.

    Only entropy_threshold/entropy_scalar_floor are varied here (matches
    the eval_strategy="cpcv_oos_sharpe" contract for those two
    parameters specifically -- a different parameter would need its own
    harness function with its own re-scaling logic).
    """
    if features_cfg is None:
        features_cfg = FeatureSettings()

    folds = _make_folds(len(samples), features_cfg.cpcv_n_splits, features_cfg.purge_gap_bars)

    champion_fold_sharpes: list[float] = []
    challenger_fold_sharpes: list[float] = []
    champion_fold_dd: list[float] = []
    challenger_fold_dd: list[float] = []
    champion_wins = 0
    challenger_wins = 0
    total_trades = 0

    for start, end in folds:
        fold_samples = samples[start:end]
        if not fold_samples:
            continue

        champion_returns = [
            s.raw_return * _position_scalar(s.entropy, champion_threshold, champion_floor)
            for s in fold_samples
        ]
        challenger_returns = [
            s.raw_return * _position_scalar(s.entropy, challenger_threshold, challenger_floor)
            for s in fold_samples
        ]

        champion_fold_sharpes.append(_fold_sharpe(champion_returns))
        challenger_fold_sharpes.append(_fold_sharpe(challenger_returns))
        champion_fold_dd.append(_max_drawdown_inverted(champion_returns))
        challenger_fold_dd.append(_max_drawdown_inverted(challenger_returns))

        champion_wins += sum(1 for r in champion_returns if r > 0)
        challenger_wins += sum(1 for r in challenger_returns if r > 0)
        total_trades += len(fold_samples)

    evaluator = ChallengerEvaluator()
    comparisons = [
        evaluator.compare_metric("oos_sharpe", champion_fold_sharpes, challenger_fold_sharpes),
        evaluator.compare_metric("max_drawdown_inverted", champion_fold_dd, challenger_fold_dd),
        evaluator.compare_proportion(
            "win_rate",
            champion_p=champion_wins / total_trades if total_trades else 0.0,
            champion_n=total_trades,
            challenger_p=challenger_wins / total_trades if total_trades else 0.0,
            challenger_n=total_trades,
        ),
    ]
    return comparisons


def _fold_sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.stdev(returns)
    if stdev <= 0.0:
        return 0.0
    return mean / stdev


def default_hmm_cfg_for(threshold: float, floor: float) -> HMMSettings:
    """Convenience for callers that want a full HMMSettings with just the
    entropy fields overridden, e.g. for logging/audit evidence."""
    base = HMMSettings()
    return base.model_copy(update={"entropy_threshold": threshold, "entropy_scalar_floor": floor})
