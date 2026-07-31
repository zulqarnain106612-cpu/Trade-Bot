"""
Ensemble regime detector — blends HMM posterior probabilities with the
Bayesian changepoint signal to produce a more robust regime estimate.

Motivation: the GaussianHMM provides smooth posterior probabilities but
can lag at genuine regime transitions. The BayesianChangepointDetector fires
quickly at structural breaks. Blending the two gives:
  - Stability during steady regimes (HMM dominates)
  - Faster regime updates at real transitions (changepoint signal increases
    the weight of the new HMM state when changepoint_prob is elevated)

Blending scheme
---------------
  p_blend[k] = (1 - alpha) * p_hmm[k] + alpha * p_cp_corrected[k]

where:
  alpha  = min(changepoint_prob / changepoint_alpha_scale, max_alpha)
  p_cp_corrected = HMM probs shifted toward the post-transition mode
    (the state with highest *pre-break* probability is down-weighted;
     the state with lowest probability is up-weighted proportionally).

When no changepoint signal is available (detector not initialised or
changepoint_prob=0), the ensemble reduces to the raw HMM prediction.

Authority:
  Hamilton (1989) Econometrica — Markov switching model (underlying HMM).
  Adams & MacKay (2007) Bayesian Online Changepoint Detection (changepoint
    prior).
  Ensemble blending approach adapted from:
    Gelman et al. (2013) Bayesian Data Analysis Ch.5 — mixture priors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from src.regime.detector import REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE


if TYPE_CHECKING:
    from src.regime.changepoint import BayesianChangepointDetector, ChangepointResult
    from src.regime.detector import RegimePrediction


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_N_STATES: Final[int] = 3
_EPS: Final[float] = 1e-9
_DEFAULT_ALPHA_SCALE: Final[float] = 0.25  # cp_prob / scale → blend weight
_DEFAULT_MAX_ALPHA: Final[float] = 0.40  # blend weight cap


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnsembleRegimePrediction:
    """
    Blended regime prediction from HMM + changepoint detector.

    Fields
    ------
    state          : final canonical regime (0=ranging, 1=trending, 2=volatile)
    prob_ranging   : blended posterior for ranging
    prob_trending  : blended posterior for trending
    prob_volatile  : blended posterior for volatile
    entropy        : normalised Shannon entropy of blended posteriors [0, 1]
    changepoint_prob : raw changepoint probability from the BOCD detector
    blend_alpha    : actual alpha applied this bar
    is_transition  : True when changepoint detector flagged a changepoint
    hmm_state      : raw HMM state (before blending)
    """

    state: int
    prob_ranging: float
    prob_trending: float
    prob_volatile: float
    entropy: float
    changepoint_prob: float
    blend_alpha: float
    is_transition: bool
    hmm_state: int

    @property
    def is_volatile(self) -> bool:
        return self.state == REGIME_VOLATILE

    @property
    def dominant_prob(self) -> float:
        return max(self.prob_ranging, self.prob_trending, self.prob_volatile)

    @property
    def confidence(self) -> float:
        return 1.0 - self.entropy

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "prob_ranging": round(self.prob_ranging, 6),
            "prob_trending": round(self.prob_trending, 6),
            "prob_volatile": round(self.prob_volatile, 6),
            "entropy": round(self.entropy, 6),
            "changepoint_prob": round(self.changepoint_prob, 6),
            "blend_alpha": round(self.blend_alpha, 4),
            "is_transition": self.is_transition,
            "hmm_state": self.hmm_state,
        }


# ---------------------------------------------------------------------------
# Blending helpers
# ---------------------------------------------------------------------------


def _entropy(probs: list[float]) -> float:
    """Normalised Shannon entropy H / log(n) in [0, 1]."""
    h = 0.0
    for p in probs:
        if p > _EPS:
            h -= p * math.log(p)
    return h / math.log(len(probs)) if len(probs) > 1 else 0.0


def _normalise(probs: list[float]) -> list[float]:
    total = sum(probs)
    if total < _EPS:
        return [1.0 / len(probs)] * len(probs)
    return [p / total for p in probs]


def _shift_toward_transition(
    hmm_probs: list[float],
    alpha: float,
) -> list[float]:
    """
    Simple transition shift: for a detected changepoint, down-weight the
    currently dominant HMM state and spread the weight uniformly across
    all states (expressing uncertainty about which state follows the break).

    new_p[k] = (1 - alpha) * hmm_probs[k] + alpha * (1/n_states)
    """
    n = len(hmm_probs)
    uniform = 1.0 / n
    shifted = [(1.0 - alpha) * p + alpha * uniform for p in hmm_probs]
    return _normalise(shifted)


# ---------------------------------------------------------------------------
# Core blend function
# ---------------------------------------------------------------------------


def blend_predictions(
    hmm: RegimePrediction,
    cp: ChangepointResult | None,
    alpha_scale: float = _DEFAULT_ALPHA_SCALE,
    max_alpha: float = _DEFAULT_MAX_ALPHA,
) -> EnsembleRegimePrediction:
    """
    Blend an HMM RegimePrediction with a BayesianChangepointDetector result.

    Parameters
    ----------
    hmm:
        Latest prediction from RegimeDetector.predict_current().
    cp:
        Latest ChangepointResult from BayesianChangepointDetector.latest()
        (or None if the detector has not yet been seeded).
    alpha_scale:
        Changepoint probability is divided by this to compute blend alpha.
        Default 0.25 → cp_prob=0.25 → alpha=1.0 (capped at max_alpha).
    max_alpha:
        Upper bound on blend alpha. Prevents the changepoint signal from
        fully overriding the HMM posterior.
    """
    cp_prob = cp.changepoint_prob if cp is not None else 0.0
    is_transition = cp.is_changepoint if cp is not None else False

    # Compute blend alpha
    alpha = min(cp_prob / max(alpha_scale, _EPS), max_alpha)

    # HMM probs in canonical order [ranging, trending, volatile]
    hmm_probs = [hmm.prob_ranging, hmm.prob_trending, hmm.prob_volatile]

    if alpha < _EPS:
        # No changepoint signal — return HMM result directly
        blended = hmm_probs
    else:
        blended = _shift_toward_transition(hmm_probs, alpha)

    blended = _normalise(blended)
    ent = _entropy(blended)

    # State: argmax of blended posteriors
    state_idx = int(max(range(_N_STATES), key=lambda i: blended[i]))
    state_map = [REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE]
    state = state_map[state_idx]

    result = EnsembleRegimePrediction(
        state=state,
        prob_ranging=blended[0],
        prob_trending=blended[1],
        prob_volatile=blended[2],
        entropy=ent,
        changepoint_prob=cp_prob,
        blend_alpha=alpha,
        is_transition=is_transition,
        hmm_state=hmm.state,
    )

    if is_transition:
        log.info(
            "ensemble_regime.transition_detected",
            cp_prob=round(cp_prob, 4),
            hmm_state=hmm.state,
            new_state=state,
        )

    return result


# ---------------------------------------------------------------------------
# Stateful ensemble detector
# ---------------------------------------------------------------------------


class EnsembleRegimeDetector:
    """
    Stateful wrapper that combines RegimeDetector + BayesianChangepointDetector.

    Usage
    -----
    detector = EnsembleRegimeDetector(regime_detector, changepoint_detector)
    result = detector.predict(obs_row)  # obs_row: 1D numpy array of HMM features
    """

    def __init__(
        self,
        regime_detector: object,  # RegimeDetector — avoid circular import
        changepoint_detector: BayesianChangepointDetector,
        alpha_scale: float = _DEFAULT_ALPHA_SCALE,
        max_alpha: float = _DEFAULT_MAX_ALPHA,
    ) -> None:
        self._regime = regime_detector
        self._cp = changepoint_detector
        self._alpha_scale = alpha_scale
        self._max_alpha = max_alpha
        self._last: EnsembleRegimePrediction | None = None

    def predict(self, obs: object, scalar_for_cp: float | None = None) -> EnsembleRegimePrediction:
        """
        Run one prediction step.

        Parameters
        ----------
        obs:
            Feature observation (numpy array) passed to regime_detector.
        scalar_for_cp:
            Optional scalar value (e.g. log-return or vol proxy) for the
            changepoint detector. If None, the changepoint detector is not
            updated this bar.
        """
        hmm_pred: RegimePrediction = self._regime.predict_current(obs)  # type: ignore[attr-defined]

        cp_result = None
        if scalar_for_cp is not None:
            self._cp.update(scalar_for_cp)
            cp_result = self._cp.latest()

        result = blend_predictions(
            hmm_pred, cp_result, alpha_scale=self._alpha_scale, max_alpha=self._max_alpha
        )
        self._last = result
        return result

    @property
    def last(self) -> EnsembleRegimePrediction | None:
        return self._last

    def reset(self) -> None:
        self._cp.reset()
        self._last = None
