"""
Champion-vs-challenger statistical evaluator for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESIGN.md §4 ("Evaluation method").

Uses the same statistical family already trusted in
src/risk/performance_drift.py (Welch t-test for continuous metrics,
two-proportion z-test for rate metrics) so there is one audited
significance-testing approach in the codebase, not two competing ones.

This module does not run backtests itself -- it takes pre-computed metric
samples (per-fold or per-trade values) for champion and challenger and
decides, per metric, whether the difference is a real (statistically
significant) improvement, a real regression, or noise. The caller
(Phase 2+ orchestration, or a future CPCV backtest harness) is
responsible for producing those samples out-of-sample per §4.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Final

from scipy.stats import norm, ttest_ind


_SIGNIFICANCE_ALPHA: Final[float] = 0.05

# Metrics where a HIGHER value is better (Sharpe, win rate, accuracy).
# Metrics where a LOWER value is better (drawdown, calibration error) must
# be negated by the caller before passing in, so this module only ever
# reasons about "higher is better."
HIGHER_IS_BETTER: Final[bool] = True


class InsufficientSampleError(ValueError):
    """Raised when a metric sample is too small to support a meaningful test."""


_MIN_SAMPLE_SIZE: Final[int] = 10


@dataclass(frozen=True)
class MetricComparison:
    metric_name: str
    champion_mean: float
    challenger_mean: float
    delta: float
    p_value: float
    significant_improvement: bool
    significant_regression: bool


@dataclass(frozen=True)
class EvaluationResult:
    param_name: str
    challenger_value: float
    comparisons: tuple[MetricComparison, ...]

    @property
    def any_significant_regression(self) -> bool:
        return any(c.significant_regression for c in self.comparisons)

    def improved(self, metric_name: str) -> bool:
        for c in self.comparisons:
            if c.metric_name == metric_name:
                return c.significant_improvement
        raise KeyError(f"metric {metric_name!r} not in evaluation result")


class ChallengerEvaluator:
    """
    Compares champion vs. challenger metric samples using Welch's t-test
    (unequal variance, appropriate when champion/challenger sample sizes
    or variances differ -- e.g. different numbers of CPCV folds).
    """

    def compare_metric(
        self, metric_name: str, champion_samples: list[float], challenger_samples: list[float]
    ) -> MetricComparison:
        if len(champion_samples) < _MIN_SAMPLE_SIZE or len(challenger_samples) < _MIN_SAMPLE_SIZE:
            raise InsufficientSampleError(
                f"{metric_name!r}: need >= {_MIN_SAMPLE_SIZE} samples per arm, got "
                f"champion={len(champion_samples)}, challenger={len(challenger_samples)}"
            )

        champion_mean = statistics.mean(champion_samples)
        challenger_mean = statistics.mean(challenger_samples)
        delta = challenger_mean - champion_mean

        # Welch's t-test: does not assume equal variance between arms.
        _, p_value_two_sided = ttest_ind(challenger_samples, champion_samples, equal_var=False)
        p_value_two_sided = float(p_value_two_sided)
        # One-tailed p-value in the direction of the observed delta.
        p_value_one_tailed = p_value_two_sided / 2.0

        significant = p_value_one_tailed < _SIGNIFICANCE_ALPHA and not math.isnan(
            p_value_one_tailed
        )
        significant_improvement = significant and delta > 0
        significant_regression = significant and delta < 0

        return MetricComparison(
            metric_name=metric_name,
            champion_mean=champion_mean,
            challenger_mean=challenger_mean,
            delta=delta,
            p_value=p_value_one_tailed,
            significant_improvement=significant_improvement,
            significant_regression=significant_regression,
        )

    def compare_proportion(
        self,
        metric_name: str,
        champion_p: float,
        champion_n: int,
        challenger_p: float,
        challenger_n: int,
    ) -> MetricComparison:
        """Two-proportion z-test, for rate metrics like win rate / accuracy."""
        if champion_n < _MIN_SAMPLE_SIZE or challenger_n < _MIN_SAMPLE_SIZE:
            raise InsufficientSampleError(
                f"{metric_name!r}: need >= {_MIN_SAMPLE_SIZE} observations per arm, got "
                f"champion={champion_n}, challenger={challenger_n}"
            )

        delta = challenger_p - champion_p
        pooled = (champion_p * champion_n + challenger_p * challenger_n) / (
            champion_n + challenger_n
        )
        se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / champion_n + 1.0 / challenger_n))

        if se <= 0.0:
            # Degenerate variance (e.g. both arms 0% or 100%) -- no meaningful
            # test can be run; treat as not significant either way.
            return MetricComparison(
                metric_name=metric_name,
                champion_mean=champion_p,
                challenger_mean=challenger_p,
                delta=delta,
                p_value=1.0,
                significant_improvement=False,
                significant_regression=False,
            )

        z = delta / se
        p_value = float(norm.sf(abs(z)))
        significant = p_value < _SIGNIFICANCE_ALPHA

        return MetricComparison(
            metric_name=metric_name,
            champion_mean=champion_p,
            challenger_mean=challenger_p,
            delta=delta,
            p_value=p_value,
            significant_improvement=significant and delta > 0,
            significant_regression=significant and delta < 0,
        )

    def evaluate(
        self, param_name: str, challenger_value: float, comparisons: list[MetricComparison]
    ) -> EvaluationResult:
        return EvaluationResult(
            param_name=param_name,
            challenger_value=challenger_value,
            comparisons=tuple(comparisons),
        )
