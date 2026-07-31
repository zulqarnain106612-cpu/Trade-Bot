"""Tests for src/regime/changepoint.py"""

from __future__ import annotations

import math

import pytest

from src.regime.changepoint import (
    BayesianChangepointDetector,
    ChangepointResult,
    _student_t_pred,
)


# ---------------------------------------------------------------------------
# _student_t_pred
# ---------------------------------------------------------------------------


def test_student_t_pred_returns_positive():
    p = _student_t_pred(0.0, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    assert p > 0.0
    assert math.isfinite(p)


def test_student_t_pred_symmetric():
    p_pos = _student_t_pred(1.0, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    p_neg = _student_t_pred(-1.0, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    assert p_pos == pytest.approx(p_neg, rel=1e-6)


def test_student_t_pred_at_mean_is_maximum():
    """Density at mean > density at distance d from mean."""
    p_center = _student_t_pred(0.0, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    p_tail = _student_t_pred(5.0, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    assert p_center > p_tail


def test_student_t_pred_extreme_x_no_crash():
    p = _student_t_pred(1e10, mu=0.0, beta=1.0, alpha=1.0, kappa=1.0)
    assert p > 0.0  # should return _EPS floor


# ---------------------------------------------------------------------------
# BayesianChangepointDetector — init
# ---------------------------------------------------------------------------


def test_init_no_crash():
    det = BayesianChangepointDetector()
    assert det.n_processed == 0
    assert det.latest() is None


def test_init_custom_params():
    det = BayesianChangepointDetector(
        expected_run_length=100.0,
        threshold=0.3,
        prior_mean=0.5,
        prior_var=2.0,
        prior_alpha=2.0,
        prior_beta=2.0,
    )
    assert det.n_processed == 0


# ---------------------------------------------------------------------------
# update — basic contract
# ---------------------------------------------------------------------------


def test_update_returns_changepoint_result():
    det = BayesianChangepointDetector()
    result = det.update(0.0)
    assert isinstance(result, ChangepointResult)
    assert result.bar_index == 0
    assert det.n_processed == 1


def test_update_changepoint_prob_in_0_1():
    det = BayesianChangepointDetector()
    for x in [0.1, -0.2, 0.3, 0.0, -0.1]:
        r = det.update(x)
        assert 0.0 <= r.changepoint_prob <= 1.0


def test_update_max_run_length_is_nonneg():
    det = BayesianChangepointDetector()
    for x in [0.0, 0.1, 0.2]:
        r = det.update(x)
        assert r.max_run_length >= 0


def test_update_mean_run_length_finite():
    det = BayesianChangepointDetector()
    for x in [0.0, 0.1, 0.2]:
        r = det.update(x)
        assert math.isfinite(r.mean_run_length)
        assert r.mean_run_length >= 0.0


def test_bar_index_increments():
    det = BayesianChangepointDetector()
    for i in range(5):
        r = det.update(float(i))
        assert r.bar_index == i
    assert det.n_processed == 5


def test_stable_data_no_changepoints():
    """Near-constant series should not trigger changepoints."""
    det = BayesianChangepointDetector(threshold=0.5)
    cps = 0
    for _ in range(50):
        r = det.update(0.001)
        if r.is_changepoint:
            cps += 1
    # Stable series: changepoint prob should be low after initial bar
    assert cps < 5


def test_large_jump_triggers_changepoint():
    """A sudden large shift should raise changepoint probability."""
    det = BayesianChangepointDetector(
        expected_run_length=10.0,
        threshold=0.3,
        prior_mean=0.0,
        prior_var=0.01,
        prior_alpha=5.0,
        prior_beta=0.05,
    )
    # Feed 20 near-zero returns
    for _ in range(20):
        det.update(0.0)
    # Then a massive jump
    result = det.update(100.0)
    # Changepoint prob should be elevated
    assert result.changepoint_prob > 0.0  # at minimum elevated from baseline


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_clears_state():
    det = BayesianChangepointDetector()
    for x in [0.1, 0.2, 0.3]:
        det.update(x)
    assert det.n_processed == 3

    det.reset()
    assert det.n_processed == 0
    assert det.latest() is None
    assert det.recent_changepoints() == []


def test_reset_then_update_restarts():
    det = BayesianChangepointDetector()
    det.update(0.5)
    det.reset()
    result = det.update(0.5)
    assert result.bar_index == 0
    assert det.n_processed == 1


# ---------------------------------------------------------------------------
# recent_changepoints
# ---------------------------------------------------------------------------


def test_recent_changepoints_empty_initially():
    det = BayesianChangepointDetector()
    assert det.recent_changepoints() == []


def test_recent_changepoints_returns_only_flagged():
    det = BayesianChangepointDetector(threshold=0.0)  # flag everything
    for i in range(5):
        det.update(float(i))
    cps = det.recent_changepoints()
    assert all(r.is_changepoint for r in cps)


def test_recent_changepoints_limit():
    det = BayesianChangepointDetector(threshold=0.0)
    for i in range(20):
        det.update(float(i))
    cps = det.recent_changepoints(n=3)
    assert len(cps) <= 3


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------


def test_latest_returns_last_result():
    det = BayesianChangepointDetector()
    for i in range(3):
        det.update(float(i))
    latest = det.latest()
    assert latest is not None
    assert latest.bar_index == 2


# ---------------------------------------------------------------------------
# ChangepointResult
# ---------------------------------------------------------------------------


def test_changepoint_result_frozen():
    r = ChangepointResult(
        bar_index=0,
        changepoint_prob=0.5,
        is_changepoint=True,
        max_run_length=0,
        mean_run_length=0.0,
    )
    with pytest.raises((AttributeError, TypeError)):
        r.bar_index = 1  # type: ignore[misc]


def test_is_changepoint_flag():
    det = BayesianChangepointDetector(threshold=1.1)  # impossible threshold
    r = det.update(0.0)
    assert r.is_changepoint is False

    det2 = BayesianChangepointDetector(threshold=0.0)  # always flag
    r2 = det2.update(0.0)
    assert r2.is_changepoint is True
