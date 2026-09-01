"""Tests for src/intelligence/probabilistic.py (40% → target 85%+)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intelligence.probabilistic import (
    BayesianExchangeStressModel,
    BayesianRegimeDetection,
    BayesianWhaleActivityModel,
    ProbabilisticPrediction,
    RiskAssessment,
)

# ---------------------------------------------------------------------------
# ProbabilisticPrediction
# ---------------------------------------------------------------------------


class TestProbabilisticPrediction:
    def _make(self, point=0.5, lower=0.3, upper=0.7, confidence=0.8):
        return ProbabilisticPrediction(
            point_estimate=point,
            lower_credible_interval=lower,
            upper_credible_interval=upper,
            confidence=confidence,
        )

    def test_credible_interval_width(self):
        p = self._make(0.5, 0.3, 0.7)
        assert p.credible_interval_width == pytest.approx(0.4)

    def test_is_confident_true(self):
        # Width 0.04 < 20% of 0.5 = 0.1
        p = self._make(0.5, 0.48, 0.52)
        assert p.is_confident is True

    def test_is_confident_false_wide_interval(self):
        p = self._make(0.5, 0.1, 0.9)
        assert p.is_confident is False

    def test_is_confident_false_near_zero(self):
        # point_estimate ~0 → is_confident always False
        p = self._make(point=0.001, lower=-0.001, upper=0.003)
        assert p.is_confident is False

    def test_decision_weight(self):
        p = self._make(point=0.8, confidence=0.75)
        assert p.decision_weight() == pytest.approx(0.8 * 0.75)

    def test_zero_confidence_zero_weight(self):
        p = self._make(point=1.0, confidence=0.0)
        assert p.decision_weight() == 0.0


# ---------------------------------------------------------------------------
# RiskAssessment
# ---------------------------------------------------------------------------


def test_risk_assessment_instantiation():
    ra = RiskAssessment(
        value_at_risk_95=-0.05,
        conditional_var_95=-0.08,
        probability_of_drawdown_gt_20pct=0.10,
        stress_test_loss={"crash": -0.30},
        regime="bull",
        regime_transition_prob_24h=0.15,
        recommendation="HOLD",
        confidence_in_rec=0.7,
    )
    assert ra.regime == "bull"
    assert ra.recommendation == "HOLD"


# ---------------------------------------------------------------------------
# BayesianExchangeStressModel
# ---------------------------------------------------------------------------


class TestBayesianExchangeStressModel:
    def setup_method(self):
        self.model = BayesianExchangeStressModel()

    def test_predict_failure_probability_normal_conditions(self):
        result = self.model.predict_failure_probability(
            netflow_zscore=0.0,
            funding_rate=0.01,
            basis_spread=5.0,
            reserve_ratio=0.5,
        )
        assert isinstance(result, ProbabilisticPrediction)
        assert 0.0 < result.point_estimate < 1.0
        assert result.lower_credible_interval <= result.point_estimate
        assert result.point_estimate <= result.upper_credible_interval
        assert 0.0 <= result.confidence <= 1.0

    def test_stress_conditions_raise_probability(self):
        normal = self.model.predict_failure_probability(0.0, 0.01, 5.0, 0.5)
        stressed = self.model.predict_failure_probability(-3.0, 0.15, 200.0, 0.05)
        assert stressed.point_estimate > normal.point_estimate

    def test_cromwells_rule_no_certainty(self):
        # Extreme stress: P should never be exactly 1.0
        result = self.model.predict_failure_probability(-10.0, 1.0, 1000.0, 0.0)
        assert result.point_estimate < 1.0
        assert result.point_estimate > 0.0

    def test_compute_logit_returns_float(self):
        logit = self.model._compute_logit(-1.0, 0.05, 10.0, 0.4)
        assert isinstance(logit, float)

    def test_credible_interval_ordered(self):
        result = self.model.predict_failure_probability(0.5, 0.02, 10.0, 0.45)
        assert result.lower_credible_interval <= result.upper_credible_interval

    def test_confidence_within_bounds(self):
        result = self.model.predict_failure_probability(0.0, 0.01, 5.0, 0.5)
        assert 0.0 <= result.confidence <= 1.0

    def test_extreme_negative_funding_widens_interval_not_narrows(self):
        """UI-011: extremity previously omitted abs() on funding/basis, so a
        large NEGATIVE funding rate (a real crisis signal, e.g. a short
        squeeze) reduced `extremity` and therefore INCREASED n_eff --
        producing an artificially narrower, overconfident interval instead
        of the wider one the docstring requires for unprecedented inputs."""
        mild = self.model.predict_failure_probability(
            netflow_zscore=0.0, funding_rate=0.01, basis_spread=5.0, reserve_ratio=0.5
        )
        extreme_negative_funding = self.model.predict_failure_probability(
            netflow_zscore=0.0, funding_rate=-0.5, basis_spread=5.0, reserve_ratio=0.5
        )
        mild_width = mild.upper_credible_interval - mild.lower_credible_interval
        extreme_width = (
            extreme_negative_funding.upper_credible_interval
            - extreme_negative_funding.lower_credible_interval
        )
        assert extreme_width > mild_width

    def test_extreme_negative_basis_widens_interval_not_narrows(self):
        mild = self.model.predict_failure_probability(
            netflow_zscore=0.0, funding_rate=0.01, basis_spread=5.0, reserve_ratio=0.5
        )
        extreme_negative_basis = self.model.predict_failure_probability(
            netflow_zscore=0.0, funding_rate=0.01, basis_spread=-300.0, reserve_ratio=0.5
        )
        mild_width = mild.upper_credible_interval - mild.lower_credible_interval
        extreme_width = (
            extreme_negative_basis.upper_credible_interval
            - extreme_negative_basis.lower_credible_interval
        )
        assert extreme_width > mild_width


# ---------------------------------------------------------------------------
# BayesianWhaleActivityModel
# ---------------------------------------------------------------------------


class TestBayesianWhaleActivityModel:
    def setup_method(self):
        self.model = BayesianWhaleActivityModel()

    def test_estimate_true_ratio_small_sample(self):
        result = self.model.estimate_true_ratio(observed_ratio=3.0, sample_size=5)
        assert isinstance(result, ProbabilisticPrediction)
        # Small sample: posterior pulled toward prior (1.5)
        assert result.point_estimate < 3.0

    def test_estimate_true_ratio_large_sample(self):
        result = self.model.estimate_true_ratio(observed_ratio=3.0, sample_size=1000)
        # Large sample: posterior close to observed
        assert result.point_estimate > 2.5

    def test_large_sample_tighter_ci(self):
        small = self.model.estimate_true_ratio(2.0, sample_size=10)
        large = self.model.estimate_true_ratio(2.0, sample_size=500)
        assert large.credible_interval_width < small.credible_interval_width

    def test_confidence_grows_with_sample_size(self):
        r1 = self.model.estimate_true_ratio(2.0, sample_size=10)
        r2 = self.model.estimate_true_ratio(2.0, sample_size=500)
        assert r2.confidence > r1.confidence

    def test_custom_prior_mean(self):
        result = self.model.estimate_true_ratio(2.0, sample_size=10, prior_mean=1.0)
        assert isinstance(result, ProbabilisticPrediction)

    def test_credible_interval_calibration_over_many_trials(self):
        """
        Calibration check (closes a gap flagged across the probabilistic
        modules: CIs are computed but never validated for coverage): the
        stated 95% credible interval should contain the true whale ratio in
        roughly 95% of independent repeated trials, given data genuinely
        drawn around that true ratio.
        """
        from src.intelligence.calibration import coverage_frequency

        rng = np.random.default_rng(seed=7)
        true_ratio = 2.2
        sample_size = 200
        # Noise on the observed ratio must match what the model's own
        # posterior_std formula (prior_std / sqrt(1 + n/10)) implicitly
        # assumes about the likelihood, i.e. std ~ prior_std * sqrt(10/n).
        # Otherwise this is testing an arbitrary external noise model rather
        # than the model's own internal calibration.
        observation_std = self.model.prior_std * (10.0 / sample_size) ** 0.5
        intervals = []
        true_values = []
        for _ in range(500):
            observed = float(rng.normal(true_ratio, observation_std))
            result = self.model.estimate_true_ratio(observed, sample_size=sample_size)
            intervals.append((result.lower_credible_interval, result.upper_credible_interval))
            true_values.append(true_ratio)

        coverage = coverage_frequency(intervals, true_values)
        assert 0.85 <= coverage <= 1.0

    def test_estimate_market_impact_bull(self):
        result = self.model.estimate_market_impact(whale_ratio=2.0, market_regime="bull")
        assert result["causal_effect"] < 0  # Volatility reduction
        assert result["effect_ci_lower"] < result["causal_effect"]
        assert result["effect_ci_upper"] > result["causal_effect"]

    def test_estimate_market_impact_bear(self):
        result = self.model.estimate_market_impact(1.0, "bear")
        assert "causal_effect" in result
        assert "interpretation" in result

    def test_estimate_market_impact_neutral(self):
        result = self.model.estimate_market_impact(1.5, "neutral")
        assert "interpretation" in result


# ---------------------------------------------------------------------------
# BayesianRegimeDetection
# ---------------------------------------------------------------------------


class TestBayesianRegimeDetection:
    def setup_method(self):
        self.model = BayesianRegimeDetection()

    def _bull_returns(self) -> pd.Series:
        rng = np.random.default_rng(0)
        return pd.Series(rng.normal(0.003, 0.01, 90))  # Positive drift

    def _bear_returns(self) -> pd.Series:
        rng = np.random.default_rng(1)
        return pd.Series(rng.normal(-0.003, 0.02, 90))  # Negative drift

    def _neutral_returns(self) -> pd.Series:
        rng = np.random.default_rng(2)
        return pd.Series(rng.normal(0.0, 0.01, 90))

    def test_detect_regime_returns_dict(self):
        result = self.model.detect_regime(
            returns_series=self._neutral_returns(),
            btc_dominance=47.0,
            network_activity_zscore=0.0,
            liquidation_pressure_zscore=0.0,
        )
        assert "probabilities" in result
        assert "most_likely_regime" in result
        assert "confidence" in result

    def test_probabilities_sum_to_one(self):
        result = self.model.detect_regime(self._bull_returns(), 52.0, 0.5, -0.5)
        probs = result["probabilities"]
        assert set(probs.keys()) == {"bull", "bear", "neutral"}
        assert abs(sum(probs.values()) - 1.0) < 1e-9

    def test_no_certainty_all_regimes(self):
        for returns, dom, net, liq in [
            (self._bull_returns(), 60.0, 2.0, -2.0),
            (self._bear_returns(), 35.0, -2.0, 2.0),
            (self._neutral_returns(), 47.0, 0.0, 0.0),
        ]:
            result = self.model.detect_regime(returns, dom, net, liq)
            for p in result["probabilities"].values():
                assert 0.0 < p < 1.0  # Smoothing prevents exact 0 or 1

    def test_bull_signals_tend_to_bull(self):
        result = self.model.detect_regime(
            self._bull_returns(),
            btc_dominance=55.0,
            network_activity_zscore=1.0,
            liquidation_pressure_zscore=-1.0,
        )
        assert result["most_likely_regime"] in ("bull", "neutral")

    def test_bear_signals_tend_to_bear(self):
        result = self.model.detect_regime(
            self._bear_returns(),
            btc_dominance=35.0,
            network_activity_zscore=-1.0,
            liquidation_pressure_zscore=2.0,
        )
        assert result["most_likely_regime"] in ("bear", "neutral")

    def test_confidence_clipped_below_1(self):
        result = self.model.detect_regime(self._bull_returns(), 60.0, 3.0, -3.0)
        assert result["confidence"] <= 0.98

    def test_single_observation(self):
        # n=1 → t_stat = 0
        single = pd.Series([0.05])
        result = self.model.detect_regime(single, 50.0, 0.0, 0.0)
        assert result["most_likely_regime"] in ("bull", "bear", "neutral")

    def test_empty_series_handled(self):
        # n=0 → t_stat = 0
        empty = pd.Series([], dtype=float)
        result = self.model.detect_regime(empty, 47.0, 0.0, 0.0)
        assert "most_likely_regime" in result
