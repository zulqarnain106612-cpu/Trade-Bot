"""
RISK-004 — Probabilities and confidences stay within [0, 1].

A number outside [0, 1] that is treated as a probability does not merely give
a wrong answer; in a sizer it usually gives the *largest* answer, because the
clip that was meant to sanitise it saturates at the maximum. `afml_bet_size`
had exactly that shape: `p_long = 2.0` produced an edge of 3.0, clipped to
1.0, and a full `max_fraction` position. The broken input bought the biggest
bet available.

So the rule is refusal, not clamping, wherever a probability is consumed --
and the Bayesian shrinkage primitive that produces probabilities has to
guarantee its own output is one.
"""

from __future__ import annotations

import math

import pytest

from src.intelligence.calibration import brier_score, coverage_frequency, shrink_probability
from src.strategies.position_sizing import afml_bet_size, thorp_kelly_with_variance

OUT_OF_RANGE = [-1.0, -0.001, 1.001, 2.0, 100.0]


class TestBetSizingRefusesNonProbabilities:
    @pytest.mark.parametrize("p_long", OUT_OF_RANGE)
    def test_afml_refuses_rather_than_saturating(self, p_long):
        # The regression this pins: clipping turned an impossible probability
        # into the maximum permitted bet.
        assert afml_bet_size(p_long, 100_000.0) == 0.0

    @pytest.mark.parametrize("p_long", [0.0, 0.25, 0.5])
    def test_no_edge_means_no_bet(self, p_long):
        assert afml_bet_size(p_long, 100_000.0) == 0.0

    @pytest.mark.parametrize(
        ("p_long", "expected_fraction"),
        [(0.55, 0.10), (0.60, 0.20), (0.625, 0.25), (0.70, 0.25), (1.0, 0.25)],
    )
    def test_the_edge_is_two_p_minus_one_capped_at_max_fraction(self, p_long, expected_fraction):
        capital = 100_000.0
        assert afml_bet_size(p_long, capital) == pytest.approx(capital * expected_fraction)

    @pytest.mark.parametrize("win_prob", [0.0, 1.0, -0.5, 1.5])
    def test_thorp_refuses_a_degenerate_or_impossible_win_probability(self, win_prob):
        assert thorp_kelly_with_variance(win_prob, 1.5, 100_000.0, 50_000.0) == 0.0

    def test_thorp_sizes_a_real_edge(self):
        # p=0.6, b=1.5: kelly_f = (0.6*1.5 - 0.4)/1.5 = 0.3333; half-Kelly is
        # 0.1667, under the 0.25 ceiling, so the ceiling is not what binds.
        result = thorp_kelly_with_variance(0.6, 1.5, 100_000.0, 50_000.0)
        assert result == pytest.approx(100_000.0 * (0.6 * 1.5 - 0.4) / 1.5 * 0.5)

    def test_the_kelly_ceiling_binds_on_a_large_edge(self):
        result = thorp_kelly_with_variance(
            0.95, 5.0, 100_000.0, 50_000.0, kelly_multiplier=1.0, kelly_ceiling=0.25
        )
        assert result == pytest.approx(25_000.0)


class TestShrinkageProducesARealProbability:
    @pytest.mark.parametrize("observed", [0.0, 0.25, 0.5, 0.75, 1.0])
    @pytest.mark.parametrize("n_obs", [0.0, 1.0, 10.0, 1_000.0])
    def test_the_posterior_mean_is_in_the_unit_interval(self, observed, n_obs):
        mean, std = shrink_probability(observed, n_obs)
        assert 0.0 <= mean <= 1.0
        assert math.isfinite(std)
        assert std >= 0.0

    @pytest.mark.parametrize("observed", OUT_OF_RANGE)
    def test_an_out_of_range_observation_is_clipped_into_range(self, observed):
        # Clipping is right here and refusal is right in the sizer, because
        # the two arguments differ: this is an empirical proportion that may
        # have been computed slightly outside [0, 1] by rounding, whereas the
        # sizer's input is an asserted probability.
        mean, _ = shrink_probability(observed, 10.0)
        assert 0.0 <= mean <= 1.0

    @pytest.mark.parametrize("prior_p", OUT_OF_RANGE)
    def test_an_out_of_range_prior_cannot_produce_a_posterior_above_one(self, prior_p):
        # prior_p was previously unbounded, so prior_p=2.0 gave a posterior
        # "probability" above 1.0 that then sized a bet.
        mean, _ = shrink_probability(0.5, 10.0, prior_p=prior_p)
        assert 0.0 <= mean <= 1.0

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    @pytest.mark.parametrize("field", ["observed_p", "n_obs", "prior_p", "prior_strength"])
    def test_a_non_finite_input_raises_rather_than_being_clamped(self, field, bad):
        kwargs = {"observed_p": 0.5, "n_obs": 10.0, "prior_p": 0.5, "prior_strength": 20.0}
        kwargs[field] = bad
        with pytest.raises(ValueError, match="finite"):
            shrink_probability(**kwargs)

    @pytest.mark.parametrize("prior_strength", [0.0, -1.0])
    def test_a_non_positive_prior_strength_raises(self, prior_strength):
        with pytest.raises(ValueError, match="prior_strength"):
            shrink_probability(0.5, 10.0, prior_strength=prior_strength)

    def test_shrinkage_vanishes_as_evidence_accumulates(self):
        near, _ = shrink_probability(0.9, 1e6)
        far, _ = shrink_probability(0.9, 1.0)
        assert near == pytest.approx(0.9, abs=1e-4)
        assert abs(far - 0.5) < abs(near - 0.5)

    def test_with_no_observations_the_posterior_is_the_prior(self):
        mean, _ = shrink_probability(0.9, 0.0, prior_p=0.3)
        assert mean == pytest.approx(0.3)


class TestCalibrationMetricsStayInRange:
    def test_brier_score_is_between_zero_and_one(self):
        assert brier_score([0.5, 0.5], [0.0, 1.0]) == pytest.approx(0.25)
        assert brier_score([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
        assert brier_score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)

    def test_brier_score_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError, match="same length"):
            brier_score([0.5], [0.0, 1.0])

    def test_brier_score_rejects_an_empty_sample(self):
        with pytest.raises(ValueError, match="non-empty"):
            brier_score([], [])

    def test_coverage_frequency_is_a_fraction(self):
        intervals = [(0.0, 1.0), (0.0, 1.0), (0.0, 0.1)]
        assert coverage_frequency(intervals, [0.5, 0.5, 0.5]) == pytest.approx(2 / 3)

    def test_coverage_frequency_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError, match="same length"):
            coverage_frequency([(0.0, 1.0)], [0.5, 0.5])

    def test_coverage_frequency_rejects_an_empty_sample(self):
        with pytest.raises(ValueError, match="non-empty"):
            coverage_frequency([], [])
