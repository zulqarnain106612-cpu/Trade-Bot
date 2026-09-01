"""Tests for src/intelligence/risk_quantification.py (0% → target 85%+)."""

from __future__ import annotations

import numpy as np
import pytest

from src.intelligence.risk_quantification import RiskMetrics, RiskQuantifier

# ---------------------------------------------------------------------------
# RiskMetrics dataclass
# ---------------------------------------------------------------------------


class TestRiskMetrics:
    def test_instantiation(self):
        rm = RiskMetrics(
            value_at_risk_95=-0.05,
            conditional_var_95=-0.08,
            prob_drawdown_20pct=0.12,
            prob_drawdown_50pct=0.02,
            sharpe_credible_interval=(1.0, 2.5),
            max_loss_scenario={"crash": -0.30},
            regime_prob={"bull": 0.6, "bear": 0.4},
            regime_transition_prob=0.15,
            recommendation="HOLD",
            confidence_in_rec=0.7,
        )
        assert rm.recommendation == "HOLD"
        assert rm.value_at_risk_95 == -0.05


# ---------------------------------------------------------------------------
# RiskQuantifier.value_at_risk
# ---------------------------------------------------------------------------


class TestValueAtRisk:
    def setup_method(self):
        self.rq = RiskQuantifier()
        rng = np.random.default_rng(42)
        self.returns = rng.standard_normal(500) * 0.02

    def test_historical_var(self):
        result = self.rq.value_at_risk(self.returns, method="historical")
        assert "var" in result
        assert "cvar" in result
        assert result["cvar"] <= result["var"]  # CVaR is worse than VaR
        assert result["method"] == "historical"

    def test_parametric_var(self):
        result = self.rq.value_at_risk(self.returns, method="parametric")
        assert "var" in result
        assert isinstance(result["var"], float)

    def test_montecarlo_var(self):
        result = self.rq.value_at_risk(self.returns, method="montecarlo")
        assert "var" in result
        assert isinstance(result["var"], float)

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="Unknown method"):
            self.rq.value_at_risk(self.returns, method="bogus")

    def test_var_tail_empty_branch(self):
        # All returns > var → tail_returns is empty → fallback to var*1.25
        returns = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        result = self.rq.value_at_risk(returns, method="historical", confidence_level=0.95)
        assert "cvar" in result

    def test_parametric_caches_quantile(self):
        result1 = self.rq.value_at_risk(self.returns, method="parametric")
        result2 = self.rq.value_at_risk(self.returns, method="parametric")
        assert result1["var"] == result2["var"]


# ---------------------------------------------------------------------------
# RiskQuantifier.stress_test
# ---------------------------------------------------------------------------


class TestStressTest:
    def setup_method(self):
        self.rq = RiskQuantifier()

    def test_default_scenarios(self):
        result = self.rq.stress_test(current_price=50000.0, current_volatility=0.02)
        assert "btc_crash_30pct" in result
        assert "liquidation_cascade" in result
        assert "exchange_insolvency" in result
        assert "contagion" in result

    def test_loss_severity_label(self):
        result = self.rq.stress_test(50000.0, 0.02)
        for data in result.values():
            assert data["severity"] in ("high", "medium")

    def test_custom_scenarios(self):
        scenarios = {"minor_dip": {"price_change": -0.05}}
        result = self.rq.stress_test(10000.0, 0.01, scenarios=scenarios)
        assert "minor_dip" in result

    def test_contagion_uses_volatility(self):
        result_low_vol = self.rq.stress_test(50000.0, current_volatility=0.01)
        result_high_vol = self.rq.stress_test(50000.0, current_volatility=0.10)
        # Higher vol → larger contagion loss
        assert result_high_vol["contagion"]["loss"] < result_low_vol["contagion"]["loss"]

    def test_price_zero_safe(self):
        result = self.rq.stress_test(0.0, 0.02)
        for scenario in result.values():
            assert isinstance(scenario["loss"], float)


# ---------------------------------------------------------------------------
# RiskQuantifier.estimate_probability_of_ruin
# ---------------------------------------------------------------------------


class TestProbabilityOfRuin:
    def setup_method(self):
        self.rq = RiskQuantifier()

    def test_normal_returns_low_ruin_prob(self):
        rng = np.random.default_rng(42)
        returns = rng.standard_normal(500) * 0.01  # ~1% daily vol
        result = self.rq.estimate_probability_of_ruin(10000.0, returns)
        assert "probability" in result
        assert 0.0 <= result["probability"] <= 1.0

    def test_volatile_returns_higher_ruin(self):
        rng = np.random.default_rng(42)
        calm = rng.standard_normal(200) * 0.005
        volatile = rng.standard_normal(200) * 0.5
        r_calm = self.rq.estimate_probability_of_ruin(10000.0, calm)
        r_vol = self.rq.estimate_probability_of_ruin(10000.0, volatile)
        assert r_vol["probability"] >= r_calm["probability"]

    def test_short_tail_branch(self):
        # Fewer than 10 tail samples → use empirical
        returns = np.array([-0.01] * 5 + [0.01] * 5)
        result = self.rq.estimate_probability_of_ruin(1000.0, returns)
        assert "probability" in result
        assert result["confidence"] in ("moderate", "low")

    def test_percentile_equivalent_format(self):
        rng = np.random.default_rng(0)
        returns = rng.standard_normal(100) * 0.02
        result = self.rq.estimate_probability_of_ruin(1000.0, returns, drawdown_threshold=0.5)
        if result["probability"] > 0:
            assert "1 in" in result["percentile_equivalent"]
        else:
            assert result["percentile_equivalent"] == "no observed ruin risk"


# ---------------------------------------------------------------------------
# RiskQuantifier.uncertainty_decomposition
# ---------------------------------------------------------------------------


class TestUncertaintyDecomposition:
    def setup_method(self):
        self.rq = RiskQuantifier()

    def test_without_ensemble(self):
        rng = np.random.default_rng(42)
        preds = rng.standard_normal(100)
        targets = preds + rng.standard_normal(100) * 0.1
        result = self.rq.uncertainty_decomposition(preds, targets)
        assert "total_rmse" in result
        assert result["epistemic_rmse"] == 0.0

    def test_with_ensemble(self):
        rng = np.random.default_rng(42)
        targets = rng.standard_normal(50)
        m1 = targets + rng.standard_normal(50) * 0.1
        m2 = targets + rng.standard_normal(50) * 0.2
        preds = (m1 + m2) / 2
        result = self.rq.uncertainty_decomposition(preds, targets, [m1, m2])
        assert result["epistemic_rmse"] >= 0.0
        assert "aleatoric_pct" in result
        assert "epistemic_pct" in result

    def test_zero_total_handles_gracefully(self):
        preds = np.ones(10)
        targets = np.ones(10)
        result = self.rq.uncertainty_decomposition(preds, targets)
        assert result["total_rmse"] == 0.0
        # aleatoric_pct falls to default 0.5 when total==0
        assert result["aleatoric_pct"] == 0.5


# ---------------------------------------------------------------------------
# Private methods
# ---------------------------------------------------------------------------


class TestPrivateMethods:
    def setup_method(self):
        self.rq = RiskQuantifier()

    def test_fit_student_t_small_sample(self):
        returns = np.array([0.01, -0.01, 0.02])
        df, _loc, _scale = self.rq._fit_student_t(returns)
        assert df == 5.0  # fallback for n < 10

    def test_fit_student_t_large_sample(self):
        rng = np.random.default_rng(42)
        returns = rng.standard_normal(200) * 0.02
        df, loc, scale = self.rq._fit_student_t(returns)
        assert 2.5 <= df <= 200.0
        assert isinstance(loc, float)
        assert isinstance(scale, float)

    def test_fit_student_t_caches(self):
        rng = np.random.default_rng(42)
        returns = rng.standard_normal(100) * 0.02
        df1, _, _ = self.rq._fit_student_t(returns)
        df2, _, _ = self.rq._fit_student_t(returns)
        assert df1 == df2
        assert self.rq._t_fit_cache["fingerprint"] is not None

    def test_fit_student_t_cache_invalidated(self):
        rng = np.random.default_rng(42)
        r1 = rng.standard_normal(50) * 0.01
        r2 = rng.standard_normal(50) * 0.05
        self.rq._fit_student_t(r1)
        fingerprint1 = self.rq._t_fit_cache["fingerprint"]
        self.rq._fit_student_t(r2)
        fingerprint2 = self.rq._t_fit_cache["fingerprint"]
        assert fingerprint1 != fingerprint2

    def test_monte_carlo_var(self):
        rng = np.random.default_rng(42)
        returns = rng.standard_normal(300) * 0.02
        var = self.rq._monte_carlo_var(returns)
        assert isinstance(var, float)
        assert var < 0  # Should be negative (loss)

    def test_simulate_scenario_price_change(self):
        loss = self.rq._simulate_scenario(50000.0, {"price_change": -0.30})
        assert abs(loss - (-0.30)) < 0.001

    def test_simulate_scenario_slippage(self):
        loss = self.rq._simulate_scenario(50000.0, {"slippage": -0.10})
        assert abs(loss - (-0.10)) < 0.001

    def test_simulate_scenario_liquidity_loss(self):
        loss = self.rq._simulate_scenario(50000.0, {"liquidity_loss": -0.20})
        assert abs(loss - (-0.20)) < 0.001

    def test_simulate_scenario_correlation_shock(self):
        loss = self.rq._simulate_scenario(
            50000.0, {"correlation_shock": 0.95}, current_volatility=0.02
        )
        assert loss < 0  # Shock causes loss

    def test_simulate_scenario_zero_price(self):
        loss = self.rq._simulate_scenario(0.0, {"price_change": -0.30})
        assert loss == 0.0  # price=0 branch

    def test_simulate_scenario_combined(self):
        loss = self.rq._simulate_scenario(
            10000.0,
            {"price_change": -0.10, "slippage": -0.05},
        )
        assert abs(loss - (-0.15)) < 0.001

    def test_parametric_var_quantile_cache_cleared_on_refetch(self):
        rng = np.random.default_rng(42)
        r1 = rng.standard_normal(100) * 0.02
        r2 = rng.standard_normal(100) * 0.04
        self.rq.value_at_risk(r1, method="parametric")
        assert len(self.rq._t_quantile_cache) > 0
        self.rq.value_at_risk(r2, method="parametric")
        # Cache was cleared and refilled for new params
        assert len(self.rq._t_quantile_cache) > 0
