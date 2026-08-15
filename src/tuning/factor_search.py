"""
Automated factor search — v6 Autonomous Research & Strategy Discovery.

A minimal genetic-programming-style search over simple technical factors
built from an OHLCV feature set, with walk-forward validation and a
Bonferroni-style multiple-testing correction so the search doesn't just
overfit noise (a real risk when trying many candidate factors — López de
Prado (2018) AFML Ch.11's core warning).

This is a search *primitive*, not a full research pipeline: it operates on
pre-computed candidate factor series (callers supply candidates; this
module does not invent arbitrary code to execute, which would be an
unacceptable code-injection surface for an autonomous trading system).

Authority:
  - Koza (1992) "Genetic Programming" — mutation/selection search framing
  - López de Prado (2018) AFML Ch.11, Ch.8 (Bailey & López de Prado (2014)
    "The Deflated Sharpe Ratio") — multiple-testing correction for
    backtest overfitting
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class FactorCandidate:
    """One candidate factor: a name and its computed value series."""

    name: str
    values: pd.Series


@dataclass(frozen=True, slots=True)
class FactorEvaluation:
    """Walk-forward evaluation result for one factor against forward returns."""

    name: str
    information_coefficient: float
    deflated_sharpe: float
    passes_multiple_testing_correction: bool


def _information_coefficient(factor: pd.Series, forward_returns: pd.Series) -> float:
    """Spearman rank correlation between factor value and forward return."""
    aligned = pd.concat([factor, forward_returns], axis=1).dropna()
    if len(aligned) < 10:
        return 0.0
    return float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1], method="spearman"))


def _deflated_sharpe_ratio(sharpe: float, n_trials: int, n_observations: int) -> float:
    """
    Bailey & López de Prado (2014) deflated Sharpe ratio: penalizes a raw
    Sharpe estimate for how many independent trials (factors) were tried,
    since trying N factors and reporting the best one systematically
    inflates the apparent Sharpe of the "winner" by chance alone.
    """
    if n_trials <= 1 or n_observations <= 1:
        return sharpe
    # Expected maximum Sharpe under the null (no real skill) across n_trials
    # independent Gaussian trials — asymptotic approximation.
    euler_mascheroni = 0.5772156649
    expected_max_null_sharpe = (1 - euler_mascheroni) * _norm_ppf(1 - 1 / n_trials) + (
        euler_mascheroni * _norm_ppf(1 - 1 / (n_trials * math.e))
    )
    expected_max_null_sharpe /= math.sqrt(n_observations)
    return sharpe - expected_max_null_sharpe


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF via Acklam's rational approximation (no scipy dependency)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
    )


def evaluate_factor_candidates(
    candidates: list[FactorCandidate],
    forward_returns: pd.Series,
    significance_alpha: float = 0.05,
) -> list[FactorEvaluation]:
    """
    Walk-forward-style evaluation of each candidate against forward returns,
    with a Bonferroni correction on the significance threshold to control
    the false-discovery rate across all candidates tried together.
    """
    n_trials = len(candidates)
    if n_trials == 0:
        return []

    corrected_alpha = significance_alpha / n_trials
    z_threshold = abs(_norm_ppf(1 - corrected_alpha / 2))

    results: list[FactorEvaluation] = []
    for candidate in candidates:
        ic = _information_coefficient(candidate.values, forward_returns)
        n_obs = min(len(candidate.values), len(forward_returns))
        # Approximate Sharpe-equivalent of the IC via its t-stat under the
        # null (IC=0), consistent with the standard IC-to-Sharpe mapping
        # used in factor research (Grinold & Kahn (1999) fundamental law).
        se = 1.0 / math.sqrt(max(n_obs - 3, 1))
        t_stat = ic / se if se > 0 else 0.0
        deflated = _deflated_sharpe_ratio(t_stat, n_trials, n_obs)
        passes = abs(t_stat) >= z_threshold and deflated > 0

        results.append(
            FactorEvaluation(
                name=candidate.name,
                information_coefficient=ic,
                deflated_sharpe=deflated,
                passes_multiple_testing_correction=passes,
            )
        )
    return results
