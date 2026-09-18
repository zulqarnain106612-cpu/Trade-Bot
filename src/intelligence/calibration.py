"""
Shared calibration and Bayesian-shrinkage primitives.

Extracted from the Beta-conjugate pattern used by BayesianExchangeStressModel
and BayesianWhaleActivityModel (src/intelligence/probabilistic.py) so the
risk-sizing layer (Kelly, correlation, drift) can pull point estimates toward
a prior instead of trusting small samples at face value, and so calibration
(coverage, Brier score) can be checked in tests rather than assumed.

Authority:
  - Gelman et al. (2013) Bayesian Data Analysis
  - McElreath (2020) Statistical Rethinking
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import beta


def shrink_probability(
    observed_p: float,
    n_obs: float,
    prior_p: float = 0.5,
    prior_strength: float = 20.0,
) -> tuple[float, float]:
    """
    Beta-conjugate shrinkage of an observed proportion toward a prior.

    Same math as BayesianWhaleActivityModel.estimate_true_ratio's Bayesian
    update (probabilistic.py), specialised to probabilities in (0, 1) using
    the Beta distribution rather than Normal -- continuous on the open
    interval by construction, no boundary clipping needed near 0/1.

    Parameters
    ----------
    observed_p : empirical proportion (e.g. win rate), in [0, 1]
    n_obs : number of observations the empirical proportion is based on
    prior_p : prior belief about the proportion (default: uninformative 0.5)
    prior_strength : effective sample size of the prior (default: 20 —
        roughly matches the trade-count scale already used as Kelly's
        minimum-sample guard)

    Returns
    -------
    (posterior_mean, posterior_std) -- posterior_mean is the shrunk estimate;
    as n_obs -> infinity, posterior_mean -> observed_p (shrinkage vanishes).
    posterior_mean is guaranteed finite and in [0, 1]; see the guards below.

    Raises
    ------
    ValueError
        On a non-finite input, or a non-positive prior_strength.

    RISK-004. The guards are not decorative. ``np.clip(nan, 0, 1)`` is
    ``nan``, and ``max(nan, 0.0)`` returns ``nan`` (Python's ``max`` keeps
    the first argument unless the second compares greater, and nothing
    compares greater than NaN), so both of the sanitising lines below used to
    pass a NaN straight through into ``posterior_mean``. A NaN "probability"
    then reaches the sizing layer, where every ``p <= 0`` guard is False for
    it. Raising is the same choice
    ``CapitalPreservationFloor.update_equity`` makes: a corrupt measurement
    in a risk primitive is a fault to surface, not a value to clamp.

    ``prior_p`` was not bounded at all, so a caller passing 2.0 obtained a
    posterior mean above 1.0 -- a "probability" that then sized a bet.
    """
    if not all(math.isfinite(v) for v in (observed_p, n_obs, prior_p, prior_strength)):
        raise ValueError(
            "shrink_probability requires finite inputs, got "
            f"observed_p={observed_p}, n_obs={n_obs}, "
            f"prior_p={prior_p}, prior_strength={prior_strength}"
        )
    if prior_strength <= 0.0:
        raise ValueError(f"prior_strength must be > 0, got {prior_strength}")

    observed_p = float(np.clip(observed_p, 0.0, 1.0))
    prior_p = float(np.clip(prior_p, 0.0, 1.0))
    n_obs = max(float(n_obs), 0.0)
    n_eff = prior_strength + n_obs

    posterior_mean = (prior_p * prior_strength + observed_p * n_obs) / n_eff

    alpha = max(posterior_mean * n_eff, 0.5)
    beta_param = max((1.0 - posterior_mean) * n_eff, 0.5)
    posterior_std = float(beta.std(alpha, beta_param))

    return posterior_mean, posterior_std


def brier_score(probabilities: list[float], outcomes: list[float]) -> float:
    """
    Mean squared error between predicted probabilities and binary outcomes.

    0.0 is perfect calibration; 0.25 is the score of an uninformative
    always-predict-0.5 model; 1.0 is maximally miscalibrated.
    """
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes must be the same length")
    if not probabilities:
        raise ValueError("probabilities must be non-empty")

    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2))


def coverage_frequency(
    intervals: list[tuple[float, float]],
    true_values: list[float],
) -> float:
    """
    Fraction of (lower, upper) credible intervals that contain their
    corresponding true value.

    A well-calibrated 95% credible interval should show coverage_frequency
    close to 0.95 when evaluated over many independent trials.
    """
    if len(intervals) != len(true_values):
        raise ValueError("intervals and true_values must be the same length")
    if not intervals:
        raise ValueError("intervals must be non-empty")

    hits = sum(
        1
        for (lower, upper), value in zip(intervals, true_values, strict=False)
        if lower <= value <= upper
    )
    return hits / len(intervals)
