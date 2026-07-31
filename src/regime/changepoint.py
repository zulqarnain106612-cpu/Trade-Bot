"""
Bayesian Online Changepoint Detection — Adams & MacKay (2007).

Detects structural breaks in a univariate time series in real time.
Maintains a posterior distribution over "run length" (bars since the last
changepoint). When P(run_length=0 | data) exceeds a threshold, a
changepoint is signalled.

Unlike the GaussianHMM (which classifies into a fixed number of labelled
states), this module answers: "has the underlying process changed at this
bar?" — a complementary diagnostic that flags regime transitions the HMM
may be slow to confirm.

Use cases:
  • Gate new entries when a changepoint is freshly detected (avoid the
    first few bars of a new regime before the HMM re-converges).
  • Trigger an early model-retrain request when a structural break is seen.
  • Surface changepoint probability on the /debug/health endpoint.

Authority:
  Adams, R.P. & MacKay, D.J.C. (2007) "Bayesian Online Changepoint
    Detection". arXiv:0710.3742.
  Fearnhead, P. & Liu, Z. (2007) "On-line inference for multiple changepoint
    problems". J.R. Stat. Soc. B 69(4):589-605.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_EPS: Final[float] = 1e-300  # prevent log(0) underflow
_DEFAULT_HAZARD: Final[float] = 1 / 250.0  # expected regime length ~250 bars


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangepointResult:
    """Output of one update step."""

    bar_index: int
    changepoint_prob: float  # P(run_length=0 | data so far), ∈ [0, 1]
    is_changepoint: bool  # True when prob > threshold
    max_run_length: int  # Most probable run length
    mean_run_length: float  # Posterior expectation of run length


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class BayesianChangepointDetector:
    """
    Online Bayesian changepoint detector for univariate returns / vol series.

    Hazard model: constant hazard rate h = 1 / expected_run_length.
    Likelihood model: Gaussian with conjugate Normal-Inverse-Gamma prior,
    updated incrementally (no re-scan of history on each step).

    Parameters
    ----------
    expected_run_length:
        Prior expected number of bars between changepoints.
        250 is approximately one trading year of daily bars.
    threshold:
        P(changepoint) must exceed this to set is_changepoint=True.
    prior_mean, prior_var:
        Normal prior parameters for the Gaussian likelihood mean.
        Typically set from a training-set estimate of the series mean/variance.
    prior_alpha, prior_beta:
        Inverse-gamma shape / scale parameters for the variance prior.
    """

    def __init__(
        self,
        expected_run_length: float = 250.0,
        threshold: float = 0.5,
        prior_mean: float = 0.0,
        prior_var: float = 1.0,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ) -> None:
        self._h = 1.0 / max(expected_run_length, 1.0)
        self._threshold = threshold

        # Conjugate NIG prior for the run starting at t=0
        self._mu0 = prior_mean
        self._kappa0 = 1.0 / max(prior_var, _EPS)
        self._alpha0 = prior_alpha
        self._beta0 = prior_beta

        # Run-length distribution: R[r] = P(run_length=r | data)
        # Represented as parallel arrays for kappa, mu, alpha, beta
        # indexed by run length (0 = just changed, 1 = 1 bar in run, ...).
        # At initialisation there is one hypothesis: run_length=0 with P=1.
        self._R: list[float] = [1.0]  # unnormalised joint probs
        self._kappa: list[float] = [self._kappa0]
        self._mu: list[float] = [self._mu0]
        self._alpha: list[float] = [self._alpha0]
        self._beta: list[float] = [self._beta0]

        self._bar_idx: int = 0
        self._history: deque[ChangepointResult] = deque(maxlen=500)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, x: float) -> ChangepointResult:
        """
        Ingest one new observation and update run-length posterior.

        Parameters
        ----------
        x : float
            The new observation (e.g. log-return or realised vol ratio).

        Returns
        -------
        ChangepointResult with updated changepoint probability.
        """
        n = len(self._R)

        # 1. Predictive probabilities for each run length hypothesis
        pred = [
            _student_t_pred(x, self._mu[r], self._beta[r], self._alpha[r], self._kappa[r])
            for r in range(n)
        ]

        # 2. Growth probabilities — existing runs grow by one bar
        growth = [(1.0 - self._h) * self._R[r] * pred[r] for r in range(n)]

        # 3. Changepoint probability — all runs collapse to run_length=0
        cp_mass = self._h * sum(self._R[r] * pred[r] for r in range(n))

        # 4. New R: prepend the changepoint mass, append grown hypotheses
        new_R = [cp_mass, *growth]

        # 5. Normalise
        Z = sum(new_R) or _EPS
        new_R = [v / Z for v in new_R]

        # 6. Update conjugate parameters for each hypothesis
        new_kappa = [self._kappa0] + [self._kappa[r] + 1.0 for r in range(n)]
        new_mu = [self._mu0] + [
            (self._kappa[r] * self._mu[r] + x) / (self._kappa[r] + 1.0) for r in range(n)
        ]
        new_alpha = [self._alpha0] + [self._alpha[r] + 0.5 for r in range(n)]
        new_beta = [self._beta0] + [
            self._beta[r] + 0.5 * self._kappa[r] / (self._kappa[r] + 1.0) * (x - self._mu[r]) ** 2
            for r in range(n)
        ]

        self._R = new_R
        self._kappa = new_kappa
        self._mu = new_mu
        self._alpha = new_alpha
        self._beta = new_beta

        # Most-probable run length
        max_r = max(range(len(new_R)), key=lambda i: new_R[i])
        mean_rl = sum(r * p for r, p in enumerate(new_R))

        result = ChangepointResult(
            bar_index=self._bar_idx,
            changepoint_prob=new_R[0],
            is_changepoint=new_R[0] > self._threshold,
            max_run_length=max_r,
            mean_run_length=mean_rl,
        )
        self._history.append(result)
        self._bar_idx += 1

        if result.is_changepoint:
            log.info(
                "changepoint_detected",
                bar=self._bar_idx,
                prob=round(result.changepoint_prob, 4),
                max_run=max_r,
            )

        return result

    def reset(self) -> None:
        """Reset detector state (e.g. after a full model retrain)."""
        self._R = [1.0]
        self._kappa = [self._kappa0]
        self._mu = [self._mu0]
        self._alpha = [self._alpha0]
        self._beta = [self._beta0]
        self._bar_idx = 0
        self._history.clear()

    def recent_changepoints(self, n: int = 10) -> list[ChangepointResult]:
        """Return the N most recent results where is_changepoint=True."""
        return [r for r in reversed(self._history) if r.is_changepoint][:n]

    def latest(self) -> ChangepointResult | None:
        """Most recent result, or None if no data has been processed."""
        return self._history[-1] if self._history else None

    @property
    def n_processed(self) -> int:
        return self._bar_idx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _student_t_pred(
    x: float,
    mu: float,
    beta: float,
    alpha: float,
    kappa: float,
) -> float:
    """
    Predictive density of x under the Normal-Inverse-Gamma conjugate model.

    This is a Student-t distribution with 2*alpha degrees of freedom,
    location mu, and scale sqrt(beta*(1 + 1/kappa) / alpha).

    Returns max(density, _EPS) to prevent zero weights.
    """
    df = 2.0 * alpha
    scale_sq = beta * (1.0 + 1.0 / kappa) / alpha
    scale = math.sqrt(max(scale_sq, _EPS))

    # Student-t log-PDF: log G((df+1)/2) - log G(df/2) - 0.5 log(df*pi*s^2) - (df+1)/2 log(1 + t^2/df)
    t_stat = (x - mu) / scale
    t_sq = t_stat**2

    try:
        log_pdf = (
            math.lgamma((df + 1.0) / 2.0)
            - math.lgamma(df / 2.0)
            - 0.5 * math.log(df * math.pi * scale_sq)
            - (df + 1.0) / 2.0 * math.log(1.0 + t_sq / df)
        )
        return max(math.exp(log_pdf), _EPS)
    except (ValueError, OverflowError):
        return _EPS
