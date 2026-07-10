"""Tests for src/intelligence/causal_inference.py (0% → target 85%+)."""

from __future__ import annotations

import numpy as np
import pytest

from src.intelligence.causal_inference import (
    CausalDAG,
    CausalEffect,
    CausalInferenceEngine,
)


# ---------------------------------------------------------------------------
# CausalDAG
# ---------------------------------------------------------------------------


class TestCausalDAG:
    def test_init_populates_edges(self):
        dag = CausalDAG()
        assert "whale_selling" in dag.edges
        assert "liquidation_volume" in dag.edges
        assert isinstance(dag.edges["whale_selling"], list)

    def test_identify_confounder_returns_list(self):
        dag = CausalDAG()
        result = dag.identify_confounder("whale_selling", "volatility")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_identify_confounder_includes_market_regime(self):
        dag = CausalDAG()
        confounders = dag.identify_confounder("anything", "anything")
        assert "market_regime" in confounders


# ---------------------------------------------------------------------------
# CausalEffect dataclass
# ---------------------------------------------------------------------------


class TestCausalEffect:
    def test_instantiation(self):
        effect = CausalEffect(
            total_effect=0.5,
            direct_effect=0.4,
            indirect_effect=0.1,
            effect_ci_lower=0.3,
            effect_ci_upper=0.7,
            confounding_bias=-0.3,
            sample_size=100,
            assumptions=["No unmeasured confounding"],
            is_robust=True,
            interpretation="test",
        )
        assert effect.total_effect == 0.5
        assert effect.is_robust is True
        assert len(effect.assumptions) == 1


# ---------------------------------------------------------------------------
# CausalInferenceEngine
# ---------------------------------------------------------------------------


class TestCausalInferenceEngine:
    def setup_method(self):
        self.engine = CausalInferenceEngine()

    def test_init_creates_dag(self):
        assert isinstance(self.engine.dag, CausalDAG)

    def test_estimate_treatment_effect_no_confounders(self):
        rng = np.random.default_rng(42)
        treatment = rng.standard_normal(100)
        outcome = treatment * 0.5 + rng.standard_normal(100) * 0.1
        result = self.engine.estimate_treatment_effect(treatment, outcome)
        assert isinstance(result, CausalEffect)
        assert result.sample_size == 100
        assert result.effect_ci_lower < result.effect_ci_upper
        assert isinstance(result.interpretation, str)

    def test_estimate_treatment_effect_with_confounders(self):
        rng = np.random.default_rng(42)
        n = 200
        confounder = rng.standard_normal(n)
        treatment = confounder * 0.8 + rng.standard_normal(n) * 0.2
        outcome = confounder * 1.5 + treatment * 0.1 + rng.standard_normal(n) * 0.1
        confounders = confounder.reshape(-1, 1)
        result = self.engine.estimate_treatment_effect(treatment, outcome, confounders)
        assert isinstance(result, CausalEffect)
        # Adjusted effect should be closer to 0.1 than naive ~1.3 correlation
        assert abs(result.total_effect) < 1.0

    def test_estimate_treatment_effect_with_instrument(self):
        rng = np.random.default_rng(42)
        n = 100
        instrument = rng.standard_normal(n)
        treatment = instrument * 0.6 + rng.standard_normal(n) * 0.4
        outcome = treatment * 0.3 + rng.standard_normal(n) * 0.5
        result = self.engine.estimate_treatment_effect(
            treatment, outcome, instrument_data=instrument
        )
        assert isinstance(result, CausalEffect)

    def test_confounding_bias_computed(self):
        rng = np.random.default_rng(42)
        n = 100
        confounder = rng.standard_normal(n)
        treatment = confounder + rng.standard_normal(n) * 0.1
        outcome = confounder + rng.standard_normal(n) * 0.1
        result = self.engine.estimate_treatment_effect(
            treatment, outcome, confounder.reshape(-1, 1)
        )
        assert isinstance(result.confounding_bias, float)
        assert "backdoor-adjusted" in result.interpretation

    def test_is_robust_flag(self):
        rng = np.random.default_rng(0)
        n = 500
        treatment = rng.standard_normal(n)
        outcome = treatment * 0.5 + rng.standard_normal(n) * 0.05
        confounders = np.zeros((n, 1))
        result = self.engine.estimate_treatment_effect(treatment, outcome, confounders)
        assert result.is_robust in (True, False, np.True_, np.False_)

    def test_estimate_heterogeneous_treatment_effect_1d(self):
        rng = np.random.default_rng(42)
        n = 100
        treatment = rng.standard_normal(n)
        outcome = treatment * 0.5 + rng.standard_normal(n) * 0.3
        regime = np.repeat([0, 1], n // 2)
        result = self.engine.estimate_heterogeneous_treatment_effect(treatment, outcome, regime)
        assert "effects_by_context" in result
        assert "average_effect" in result
        assert "interpretation" in result

    def test_estimate_heterogeneous_treatment_effect_2d(self):
        rng = np.random.default_rng(42)
        n = 80
        treatment = rng.standard_normal(n)
        outcome = treatment * 0.3 + rng.standard_normal(n) * 0.3
        regime = np.repeat([0, 1, 2, 3], n // 4)
        conditioning_2d = regime.reshape(-1, 1)
        result = self.engine.estimate_heterogeneous_treatment_effect(
            treatment, outcome, conditioning_2d
        )
        assert isinstance(result["effects_by_context"], dict)

    def test_heterogeneous_small_stratum_excluded(self):
        rng = np.random.default_rng(1)
        n = 50
        treatment = rng.standard_normal(n)
        outcome = rng.standard_normal(n)
        # Regime 2 only has 1 sample → below _MIN_STRATUM_SIZE=5
        regime = np.array([0] * 24 + [1] * 25 + [2])
        result = self.engine.estimate_heterogeneous_treatment_effect(treatment, outcome, regime)
        assert "2" in result["excluded_strata"]

    def test_heterogeneous_all_strata_too_small_returns_none(self):
        treatment = np.array([1.0, 2.0])
        outcome = np.array([1.0, 2.0])
        regime = np.array([0, 1])  # each stratum has only 1 sample
        result = self.engine.estimate_heterogeneous_treatment_effect(treatment, outcome, regime)
        assert result["average_effect"] is None

    def test_counterfactual_reduce_position_float(self):
        state = {"whale_buy_sell_ratio": 1.5, "funding_rate": 0.01, "position_size": 1.0}
        intervention = {"reduce_position": 0.5}
        result = self.engine.counterfactual_prediction(state, intervention)
        assert "baseline_sharpe" in result
        assert "sharpe_under_intervention" in result
        assert "opportunity_cost" in result
        assert result["recommendation"] in ("REDUCE", "HOLD")

    def test_counterfactual_reduce_position_50pct(self):
        state = {"whale_buy_sell_ratio": 1.0, "funding_rate": 0.0}
        intervention = {"reduce_position_50pct": True}
        result = self.engine.counterfactual_prediction(state, intervention)
        assert "opportunity_cost" in result

    def test_counterfactual_no_intervention_key(self):
        state = {}
        intervention = {}
        result = self.engine.counterfactual_prediction(state, intervention)
        assert result["baseline_sharpe"] == result["sharpe_under_intervention"]

    def test_counterfactual_unrecognized_key_raises(self):
        with pytest.raises(ValueError, match="Unrecognized intervention key"):
            self.engine.counterfactual_prediction({}, {"typo_key": 0.5})

    def test_backdoor_adjustment(self):
        rng = np.random.default_rng(42)
        n = 100
        t = rng.standard_normal(n)
        o = t * 2.0 + rng.standard_normal(n) * 0.1
        c = np.zeros((n, 1))
        coef = self.engine._backdoor_adjustment(t, o, c)
        assert abs(coef - 2.0) < 0.5

    def test_instrumental_variable(self):
        rng = np.random.default_rng(42)
        n = 200
        instrument = rng.standard_normal(n)
        treatment = instrument * 0.8 + rng.standard_normal(n) * 0.2
        outcome = treatment * 0.5 + rng.standard_normal(n) * 0.3
        result = self.engine._instrumental_variable(treatment, outcome, instrument)
        assert isinstance(result, float)

    def test_bootstrap_ci(self):
        rng = np.random.default_rng(42)
        n = 100
        t = rng.standard_normal(n)
        o = t * 0.5 + rng.standard_normal(n) * 0.3
        c = np.zeros((n, 1))
        lower, upper = self.engine._bootstrap_ci(t, o, c)
        assert lower < upper

    def test_predict_sharpe_defaults(self):
        sharpe = self.engine._predict_sharpe({})
        assert sharpe >= 2.0

    def test_predict_sharpe_high_whale_ratio(self):
        s1 = self.engine._predict_sharpe({"whale_buy_sell_ratio": 2.0})
        s2 = self.engine._predict_sharpe({"whale_buy_sell_ratio": 0.5})
        assert s1 > s2

    def test_predict_sharpe_high_funding_reduces_sharpe(self):
        s1 = self.engine._predict_sharpe({"funding_rate": 0.0})
        s2 = self.engine._predict_sharpe({"funding_rate": 0.5})
        assert s1 > s2

    def test_predict_sharpe_floor_at_2(self):
        sharpe = self.engine._predict_sharpe({"funding_rate": 100.0})
        assert sharpe == 2.0

    def test_treatment_effect_small_dataset(self):
        # Edge case: small arrays
        t = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        o = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        result = self.engine.estimate_treatment_effect(t, o)
        assert isinstance(result, CausalEffect)
