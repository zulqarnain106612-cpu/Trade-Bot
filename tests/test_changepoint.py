"""Tests for the v4 Bayesian online changepoint detector."""

from __future__ import annotations

import random

import pytest

from src.regime.changepoint import BayesianOnlineChangepointDetector


def test_rejects_invalid_hazard_rate() -> None:
    with pytest.raises(ValueError, match="hazard_rate"):
        BayesianOnlineChangepointDetector(hazard_rate=0.0)
    with pytest.raises(ValueError, match="hazard_rate"):
        BayesianOnlineChangepointDetector(hazard_rate=1.0)


def test_stable_stream_keeps_changepoint_probability_low() -> None:
    rng = random.Random(1)
    detector = BayesianOnlineChangepointDetector(hazard_rate=1.0 / 250.0)
    probs = [detector.update(rng.gauss(0, 1)) for _ in range(150)]
    assert sum(probs[-20:]) / 20 < 0.5


def test_run_length_grows_on_stable_stream() -> None:
    rng = random.Random(2)
    detector = BayesianOnlineChangepointDetector(hazard_rate=1.0 / 250.0)
    for _ in range(100):
        detector.update(rng.gauss(0, 1))
    assert detector.most_likely_run_length > 0


def test_regime_shift_eventually_collapses_run_length() -> None:
    # Per Adams & MacKay (2007): the exact outlier step does not spike
    # changepoint_probability (it's equally surprising to every existing
    # hypothesis, so the hazard-rate ratio is preserved through
    # normalization). What DOES happen is that short-run hypotheses
    # fitted to the new cluster quickly dominate the posterior once a
    # few post-shift points confirm the new regime — observable via
    # most_likely_run_length collapsing back toward 0.
    rng = random.Random(3)
    detector = BayesianOnlineChangepointDetector(hazard_rate=1.0 / 50.0)
    for _ in range(80):
        detector.update(rng.gauss(0, 0.5))
    pre_shift_run_length = detector.most_likely_run_length
    for _ in range(15):
        detector.update(rng.gauss(20, 0.5))
    assert detector.most_likely_run_length < pre_shift_run_length


def test_changepoint_probability_property_matches_last_update() -> None:
    detector = BayesianOnlineChangepointDetector()
    p = detector.update(0.5)
    assert detector.changepoint_probability == p


def test_first_observation_changepoint_probability_matches_hazard_rate() -> None:
    # With no history, every hypothesis shares the same predictive
    # likelihood for x, so cp mass reduces to the hazard-rate prior itself.
    detector = BayesianOnlineChangepointDetector(hazard_rate=1.0 / 250.0)
    p = detector.update(1.0)
    assert p == pytest.approx(1.0 / 250.0, rel=0.05)
