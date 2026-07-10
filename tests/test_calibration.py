"""Tests for src/intelligence/calibration.py."""

from __future__ import annotations

import numpy as np
import pytest

from src.intelligence.calibration import (
    brier_score,
    coverage_frequency,
    shrink_probability,
)


class TestShrinkProbability:
    def test_shrinks_toward_prior_with_small_sample(self):
        mean, _std = shrink_probability(observed_p=0.9, n_obs=10, prior_p=0.5, prior_strength=20)
        assert 0.5 < mean < 0.9

    def test_converges_to_observed_with_large_sample(self):
        mean, _std = shrink_probability(
            observed_p=0.7, n_obs=100_000, prior_p=0.5, prior_strength=20
        )
        assert mean == pytest.approx(0.7, abs=1e-3)

    def test_zero_observations_returns_prior(self):
        mean, _std = shrink_probability(observed_p=0.9, n_obs=0, prior_p=0.5, prior_strength=20)
        assert mean == pytest.approx(0.5)

    def test_std_decreases_with_more_observations(self):
        _, std_small = shrink_probability(observed_p=0.6, n_obs=10, prior_strength=20)
        _, std_large = shrink_probability(observed_p=0.6, n_obs=1000, prior_strength=20)
        assert std_large < std_small

    def test_clips_observed_p_to_valid_range(self):
        mean, _ = shrink_probability(observed_p=1.5, n_obs=50, prior_p=0.5, prior_strength=20)
        assert 0.0 <= mean <= 1.0

    def test_output_bounded_in_unit_interval(self):
        for p in (0.0, 0.01, 0.5, 0.99, 1.0):
            mean, std = shrink_probability(observed_p=p, n_obs=50)
            assert 0.0 <= mean <= 1.0
            assert std >= 0.0


class TestBrierScore:
    def test_perfect_calibration(self):
        assert brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == pytest.approx(0.0)

    def test_uninformative_always_half(self):
        assert brier_score([0.5, 0.5], [1.0, 0.0]) == pytest.approx(0.25)

    def test_worst_case(self):
        assert brier_score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            brier_score([0.5], [1.0, 0.0])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            brier_score([], [])


class TestCoverageFrequency:
    def test_all_hits(self):
        intervals = [(0.0, 1.0), (0.4, 0.6)]
        true_values = [0.5, 0.5]
        assert coverage_frequency(intervals, true_values) == pytest.approx(1.0)

    def test_all_misses(self):
        intervals = [(0.0, 0.1), (0.9, 1.0)]
        true_values = [0.5, 0.5]
        assert coverage_frequency(intervals, true_values) == pytest.approx(0.0)

    def test_partial_coverage(self):
        intervals = [(0.0, 1.0), (0.9, 1.0)]
        true_values = [0.5, 0.5]
        assert coverage_frequency(intervals, true_values) == pytest.approx(0.5)

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            coverage_frequency([(0.0, 1.0)], [0.5, 0.5])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            coverage_frequency([], [])

    def test_beta_ci_achieves_nominal_coverage_over_many_trials(self):
        """
        Statistical property test: shrink_probability's implied 95% Beta CI
        should contain the true probability ~95% of the time when the data
        really is generated from that true probability (calibration check).
        """
        rng = np.random.default_rng(seed=42)
        from scipy.stats import beta as beta_dist

        true_p = 0.62
        n_obs = 200
        intervals: list[tuple[float, float]] = []
        true_values: list[float] = []
        for _ in range(500):
            observed = rng.binomial(n_obs, true_p) / n_obs
            mean, _std = shrink_probability(observed, n_obs=n_obs, prior_strength=20)
            n_eff = 20 + n_obs
            alpha = max(mean * n_eff, 0.5)
            beta_param = max((1.0 - mean) * n_eff, 0.5)
            lower = float(beta_dist.ppf(0.025, alpha, beta_param))
            upper = float(beta_dist.ppf(0.975, alpha, beta_param))
            intervals.append((lower, upper))
            true_values.append(true_p)

        coverage = coverage_frequency(intervals, true_values)
        assert 0.85 <= coverage <= 1.0
