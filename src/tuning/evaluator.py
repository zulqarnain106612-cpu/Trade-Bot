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

from scipy import stats as scipy_stats
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


def probabilistic_sharpe_ratio(returns: list[float], benchmark_sr: float = 0.0) -> float:
    """
    Bailey & Lopez de Prado (2012) Probabilistic Sharpe Ratio -- the
    probability that the true (population) Sharpe ratio exceeds
    `benchmark_sr`, given a finite, possibly skewed/fat-tailed sample.

    A plain Sharpe ratio comparison (as used elsewhere in this module)
    implicitly assumes i.i.d. normal returns; crypto trade returns are
    routinely skewed and fat-tailed, which inflates apparent significance
    under a naive t-test. PSR corrects for this by folding sample
    skewness and (excess) kurtosis into the standard error of the
    estimated Sharpe ratio (AFML Ch. 14 / Bailey & Lopez de Prado 2012,
    "The Sharpe Ratio Efficient Frontier").

    Returns a probability in [0, 1] -- e.g. 0.95 means "95% confident the
    true Sharpe ratio is above benchmark_sr," not a p-value.
    """
    n = len(returns)
    if n < 2:
        return 0.5  # no information to distinguish from the benchmark

    stdev = statistics.pstdev(returns)
    if stdev == 0.0:
        mean = statistics.mean(returns)
        return 1.0 if mean > benchmark_sr else 0.0

    mean = statistics.mean(returns)
    sr = mean / stdev
    skew = float(scipy_stats.skew(returns, bias=False))
    excess_kurtosis = float(scipy_stats.kurtosis(returns, fisher=True, bias=False))

    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (excess_kurtosis / 4.0) * sr**2))
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / denom
    return float(norm.cdf(z))


def deflated_sharpe_ratio(returns: list[float], n_trials: int, benchmark_sr: float = 0.0) -> float:
    """
    Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio -- PSR against a
    benchmark raised to account for selection bias from having tried
    `n_trials` independent configurations (the self-tuning loop proposes
    many challenger values over time; evaluating the best-looking one
    against a fixed benchmark without this correction systematically
    overstates significance -- "multiple testing" / backtest overfitting,
    AFML Ch. 11).

    `n_trials` should be the count of independent PROPOSED attempts on
    this parameter to date (see TuningAuditLog), not folds within a
    single backtest.
    """
    n_trials = max(1, n_trials)
    if n_trials == 1 or len(returns) < 2:
        expected_max_sr = benchmark_sr
    else:
        # Expected maximum Sharpe ratio across n_trials independent trials
        # under the null (true SR == benchmark_sr), via the standard
        # extreme-value approximation used in the DSR derivation.
        euler_gamma = 0.5772156649015329
        expected_max_z = (1.0 - euler_gamma) * norm.ppf(
            1.0 - 1.0 / n_trials
        ) + euler_gamma * norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        # The extreme-value term is in units of the *cross-trial dispersion of
        # estimated Sharpe ratios*, which under the null is approximately
        # 1/sqrt(n) for n observations. This previously scaled by
        # statistics.pstdev(returns) — the dispersion of the returns
        # themselves, a different quantity that is smaller by roughly the
        # Sharpe ratio. The deflation was therefore far too small and the
        # correction under-penalised exactly the over-fitted winners it
        # exists to catch. src/tuning/factor_search.py's sibling DSR already
        # used the 1/sqrt(n) form; the two now agree.
        sharpe_estimate_stdev = 1.0 / math.sqrt(len(returns))
        expected_max_sr = benchmark_sr + expected_max_z * sharpe_estimate_stdev

    return probabilistic_sharpe_ratio(returns, benchmark_sr=expected_max_sr)


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
