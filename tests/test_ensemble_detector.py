"""Tests for src/regime/ensemble_detector.py"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from src.regime.changepoint import BayesianChangepointDetector, ChangepointResult
from src.regime.detector import REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE
from src.regime.ensemble_detector import (
    EnsembleRegimeDetector,
    EnsembleRegimePrediction,
    _entropy,
    _normalise,
    _shift_toward_transition,
    blend_predictions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmm(state=1, pr=0.1, pt=0.8, pv=0.1, entropy=0.2):
    m = MagicMock()
    m.state = state
    m.prob_ranging = pr
    m.prob_trending = pt
    m.prob_volatile = pv
    m.entropy = entropy
    return m


def _cp_result(cp_prob=0.5, is_cp=True):
    return ChangepointResult(
        bar_index=10,
        changepoint_prob=cp_prob,
        is_changepoint=is_cp,
        max_run_length=5,
        mean_run_length=3.0,
    )


# ---------------------------------------------------------------------------
# _entropy
# ---------------------------------------------------------------------------


def test_entropy_uniform_is_one():
    probs = [1 / 3, 1 / 3, 1 / 3]
    assert _entropy(probs) == pytest.approx(1.0)


def test_entropy_certain_is_zero():
    probs = [1.0, 0.0, 0.0]
    assert _entropy(probs) == pytest.approx(0.0, abs=1e-9)


def test_entropy_in_range():
    probs = [0.6, 0.3, 0.1]
    e = _entropy(probs)
    assert 0.0 <= e <= 1.0


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------


def test_normalise_sums_to_one():
    probs = [2.0, 3.0, 5.0]
    n = _normalise(probs)
    assert sum(n) == pytest.approx(1.0)


def test_normalise_zero_vector_returns_uniform():
    n = _normalise([0.0, 0.0, 0.0])
    assert n == pytest.approx([1 / 3, 1 / 3, 1 / 3])


# ---------------------------------------------------------------------------
# _shift_toward_transition
# ---------------------------------------------------------------------------


def test_shift_alpha_zero_unchanged():
    probs = [0.1, 0.8, 0.1]
    shifted = _shift_toward_transition(probs, alpha=0.0)
    assert shifted == pytest.approx([0.1, 0.8, 0.1])


def test_shift_alpha_one_is_uniform():
    probs = [0.0, 1.0, 0.0]
    shifted = _shift_toward_transition(probs, alpha=1.0)
    assert shifted == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_shift_reduces_dominant_state():
    probs = [0.1, 0.8, 0.1]
    shifted = _shift_toward_transition(probs, alpha=0.3)
    assert shifted[1] < 0.8  # dominant state reduced


# ---------------------------------------------------------------------------
# blend_predictions — no changepoint
# ---------------------------------------------------------------------------


def test_blend_no_cp_returns_hmm_probs():
    hmm = _hmm(state=1, pr=0.1, pt=0.8, pv=0.1)
    result = blend_predictions(hmm, cp=None)
    assert result.prob_trending == pytest.approx(0.8)
    assert result.blend_alpha == pytest.approx(0.0, abs=1e-6)
    assert result.changepoint_prob == pytest.approx(0.0)
    assert result.is_transition is False


def test_blend_no_cp_hmm_state_preserved():
    hmm = _hmm(state=REGIME_TRENDING)
    result = blend_predictions(hmm, cp=None)
    assert result.state == REGIME_TRENDING


def test_blend_no_cp_prob_sum_one():
    hmm = _hmm()
    result = blend_predictions(hmm, cp=None)
    assert result.prob_ranging + result.prob_trending + result.prob_volatile == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# blend_predictions — with changepoint
# ---------------------------------------------------------------------------


def test_blend_with_cp_reduces_dominant_prob():
    hmm = _hmm(state=1, pr=0.1, pt=0.8, pv=0.1)
    cp = _cp_result(cp_prob=0.5, is_cp=True)
    result = blend_predictions(hmm, cp, alpha_scale=0.5, max_alpha=1.0)
    assert result.prob_trending < 0.8  # smoothed toward uniform


def test_blend_with_cp_is_transition_set():
    hmm = _hmm()
    cp = _cp_result(is_cp=True)
    result = blend_predictions(hmm, cp)
    assert result.is_transition is True


def test_blend_cp_prob_forwarded():
    hmm = _hmm()
    cp = _cp_result(cp_prob=0.3)
    result = blend_predictions(hmm, cp)
    assert result.changepoint_prob == pytest.approx(0.3)


def test_blend_max_alpha_capped():
    hmm = _hmm(state=1, pr=0.05, pt=0.9, pv=0.05)
    # cp_prob / alpha_scale = 1.0 / 0.1 = 10 → capped at max_alpha
    cp = _cp_result(cp_prob=1.0)
    result = blend_predictions(hmm, cp, alpha_scale=0.1, max_alpha=0.4)
    assert result.blend_alpha == pytest.approx(0.4)


def test_blend_prob_sums_to_one():
    hmm = _hmm()
    cp = _cp_result(cp_prob=0.5)
    r = blend_predictions(hmm, cp)
    assert r.prob_ranging + r.prob_trending + r.prob_volatile == pytest.approx(1.0)


def test_blend_entropy_in_range():
    hmm = _hmm()
    cp = _cp_result(cp_prob=0.5)
    r = blend_predictions(hmm, cp)
    assert 0.0 <= r.entropy <= 1.0


def test_blend_as_dict_keys():
    hmm = _hmm()
    r = blend_predictions(hmm, None)
    d = r.as_dict()
    for key in (
        "state",
        "prob_ranging",
        "prob_trending",
        "prob_volatile",
        "entropy",
        "changepoint_prob",
        "blend_alpha",
        "is_transition",
    ):
        assert key in d


def test_blend_frozen():
    r = blend_predictions(_hmm(), None)
    with pytest.raises((AttributeError, TypeError)):
        r.state = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EnsembleRegimePrediction properties
# ---------------------------------------------------------------------------


def test_is_volatile_true():
    r = EnsembleRegimePrediction(
        state=REGIME_VOLATILE,
        prob_ranging=0.1,
        prob_trending=0.1,
        prob_volatile=0.8,
        entropy=0.3,
        changepoint_prob=0.0,
        blend_alpha=0.0,
        is_transition=False,
        hmm_state=REGIME_VOLATILE,
    )
    assert r.is_volatile is True


def test_is_volatile_false():
    r = EnsembleRegimePrediction(
        state=REGIME_RANGING,
        prob_ranging=0.8,
        prob_trending=0.1,
        prob_volatile=0.1,
        entropy=0.2,
        changepoint_prob=0.0,
        blend_alpha=0.0,
        is_transition=False,
        hmm_state=REGIME_RANGING,
    )
    assert r.is_volatile is False


def test_confidence_is_one_minus_entropy():
    r = EnsembleRegimePrediction(
        state=REGIME_TRENDING,
        prob_ranging=0.1,
        prob_trending=0.8,
        prob_volatile=0.1,
        entropy=0.25,
        changepoint_prob=0.0,
        blend_alpha=0.0,
        is_transition=False,
        hmm_state=REGIME_TRENDING,
    )
    assert r.confidence == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# EnsembleRegimeDetector
# ---------------------------------------------------------------------------


def test_ensemble_detector_last_none_before_predict():
    cp_det = BayesianChangepointDetector()
    regime_mock = MagicMock()
    regime_mock.predict_current.return_value = _hmm(state=1, pr=0.1, pt=0.8, pv=0.1)
    det = EnsembleRegimeDetector(regime_mock, cp_det)
    assert det.last is None


def test_ensemble_detector_predict_returns_ensemble():
    cp_det = BayesianChangepointDetector()
    regime_mock = MagicMock()
    regime_mock.predict_current.return_value = _hmm(state=1, pr=0.1, pt=0.8, pv=0.1)
    det = EnsembleRegimeDetector(regime_mock, cp_det)
    obs = MagicMock()
    result = det.predict(obs, scalar_for_cp=0.01)
    assert isinstance(result, EnsembleRegimePrediction)
    assert det.last is result


def test_ensemble_detector_reset_clears_last():
    cp_det = BayesianChangepointDetector()
    regime_mock = MagicMock()
    regime_mock.predict_current.return_value = _hmm()
    det = EnsembleRegimeDetector(regime_mock, cp_det)
    det.predict(MagicMock(), scalar_for_cp=0.01)
    det.reset()
    assert det.last is None


def test_ensemble_detector_no_cp_update_when_no_scalar():
    cp_det = BayesianChangepointDetector()
    regime_mock = MagicMock()
    regime_mock.predict_current.return_value = _hmm(state=0, pr=0.8, pt=0.1, pv=0.1)
    det = EnsembleRegimeDetector(regime_mock, cp_det)
    result = det.predict(MagicMock(), scalar_for_cp=None)
    # No CP update → no transition
    assert result.changepoint_prob == pytest.approx(0.0)
    assert result.is_transition is False


def test_ensemble_detector_entropy_finite():
    cp_det = BayesianChangepointDetector()
    regime_mock = MagicMock()
    regime_mock.predict_current.return_value = _hmm()
    det = EnsembleRegimeDetector(regime_mock, cp_det)
    for _ in range(5):
        result = det.predict(MagicMock(), scalar_for_cp=0.01)
    assert math.isfinite(result.entropy)
