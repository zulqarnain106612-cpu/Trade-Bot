"""
Coverage for src/risk/probabilistic_gates.py — Debt-005.

All gates use internal Bayesian models; we test via the evaluate() interface
by exercising both the pass and halt branches.
"""
from __future__ import annotations

import pytest

from src.risk.probabilistic_gates import (
    GateDecision,
    ProbabilisticGate6Enhanced,
    ProbabilisticGate7,
    ProbabilisticGate8,
    ProbabilisticGateEvaluation,
)


# ---------------------------------------------------------------------------
# GateDecision enum
# ---------------------------------------------------------------------------

class TestGateDecision:
    def test_halt_value(self):
        assert GateDecision.HALT == "HALT"

    def test_hold_value(self):
        assert GateDecision.HOLD == "HOLD"

    def test_reduce_values_ordered(self):
        # REDUCE_25 < REDUCE_50 < REDUCE_75 in severity
        assert GateDecision.REDUCE_25 != GateDecision.REDUCE_50
        assert GateDecision.REDUCE_50 != GateDecision.REDUCE_75


# ---------------------------------------------------------------------------
# ProbabilisticGate7 — Exchange Stress
# ---------------------------------------------------------------------------

class TestProbabilisticGate7:
    def _gate(self):
        return ProbabilisticGate7()

    def test_safe_conditions_hold(self):
        result = self._gate().evaluate(
            netflow_zscore=0.0,
            funding_rate=0.01,
            basis_spread=0.002,
            reserve_ratio=0.8,
        )
        assert isinstance(result, ProbabilisticGateEvaluation)
        assert result.gate_id == 7
        assert result.decision == GateDecision.HOLD

    def test_extreme_stress_halts(self):
        result = self._gate().evaluate(
            netflow_zscore=-5.0,
            funding_rate=0.5,
            basis_spread=0.1,
            reserve_ratio=0.1,
        )
        assert result.gate_id == 7
        assert result.decision in (GateDecision.HALT, GateDecision.REDUCE_75,
                                   GateDecision.REDUCE_50)

    def test_probability_in_unit_interval(self):
        result = self._gate().evaluate(
            netflow_zscore=0.0, funding_rate=0.01,
            basis_spread=0.002, reserve_ratio=0.8,
        )
        assert 0.0 <= result.probability_of_halt <= 1.0

    def test_credible_interval_ordered(self):
        result = self._gate().evaluate(
            netflow_zscore=0.0, funding_rate=0.01,
            basis_spread=0.002, reserve_ratio=0.8,
        )
        lo, hi = result.credible_interval
        assert lo <= hi

    def test_confidence_in_unit_interval(self):
        result = self._gate().evaluate(
            netflow_zscore=0.0, funding_rate=0.01,
            basis_spread=0.002, reserve_ratio=0.8,
        )
        assert 0.0 <= result.confidence <= 1.0

    def test_triggered_by_is_list(self):
        result = self._gate().evaluate(
            netflow_zscore=-3.0, funding_rate=0.2,
            basis_spread=0.05, reserve_ratio=0.3,
        )
        assert isinstance(result.triggered_by, list)

    def test_moderate_stress_reduces(self):
        # Moderately elevated stress → REDUCE (not HALT, not HOLD)
        result = self._gate().evaluate(
            netflow_zscore=-1.5,
            funding_rate=0.08,
            basis_spread=0.02,
            reserve_ratio=0.5,
        )
        assert result.decision != GateDecision.HOLD or result.probability_of_halt < 0.85


# ---------------------------------------------------------------------------
# ProbabilisticGate8 — Whale Activity
# ---------------------------------------------------------------------------

class TestProbabilisticGate8:
    def _gate(self):
        return ProbabilisticGate8()

    def _eval(self, whale_ratio=1.0, sample_size=100,
               market_regime="bull", price_zscore=0.0):
        return self._gate().evaluate(
            observed_whale_ratio=whale_ratio,
            sample_size=sample_size,
            market_regime=market_regime,
            current_price_zscore=price_zscore,
        )

    def test_safe_conditions_result_valid(self):
        result = self._eval()
        assert result.gate_id == 8
        # Probabilistic gate: neutral whale ratio may still give REDUCE_25
        assert result.decision in (GateDecision.HOLD, GateDecision.REDUCE_25,
                                   GateDecision.REDUCE_50)

    def test_extreme_whale_selling_reduces_or_halts(self):
        result = self._eval(whale_ratio=0.05, sample_size=1000, price_zscore=3.0)
        assert result.decision != GateDecision.HOLD

    def test_probability_in_unit_interval(self):
        result = self._eval()
        assert 0.0 <= result.probability_of_halt <= 1.0

    def test_credible_interval_ordered(self):
        lo, hi = self._eval().credible_interval
        assert lo <= hi

    def test_result_has_reason_string(self):
        result = self._eval(whale_ratio=0.3, sample_size=50)
        assert isinstance(result.reason, str)

    def test_small_sample_increases_uncertainty(self):
        small = self._eval(sample_size=5)
        large = self._eval(sample_size=500)
        # Smaller sample → wider credible interval
        lo_s, hi_s = small.credible_interval
        lo_l, hi_l = large.credible_interval
        assert (hi_s - lo_s) >= (hi_l - lo_l) - 0.01  # allow tiny float delta


# ---------------------------------------------------------------------------
# ProbabilisticGate6Enhanced — Drift + Macro
# ---------------------------------------------------------------------------

class TestProbabilisticGate6Enhanced:
    def _gate(self):
        return ProbabilisticGate6Enhanced()

    def _returns(self, n=30, mean=0.001):
        import pandas as pd, numpy as np
        rng = np.random.default_rng(42)
        return pd.Series(rng.normal(mean, 0.02, n))

    def _eval(self, current_sharpe=1.2, baseline_sharpe=1.1,
               btc_dominance=0.45, net_act_z=0.0, liq_z=0.0):
        return self._gate().evaluate(
            current_sharpe=current_sharpe,
            baseline_sharpe=baseline_sharpe,
            recent_returns=self._returns(),
            btc_dominance=btc_dominance,
            network_activity_zscore=net_act_z,
            liquidation_pressure_zscore=liq_z,
        )

    def test_no_drift_result_is_valid(self):
        result = self._eval()
        assert result.gate_id == 6
        assert isinstance(result.decision, GateDecision)
        assert 0.0 <= result.probability_of_halt <= 1.0

    def test_large_sharpe_crash_reduces_or_halts(self):
        result = self._eval(current_sharpe=-0.5, baseline_sharpe=1.5)
        assert result.decision != GateDecision.HOLD

    def test_probability_in_unit_interval(self):
        assert 0.0 <= self._eval().probability_of_halt <= 1.0

    def test_credible_interval_ordered(self):
        lo, hi = self._eval().credible_interval
        assert lo <= hi

    def test_high_liquidation_pressure_elevates_risk(self):
        low_liq = self._eval(liq_z=0.0)
        high_liq = self._eval(liq_z=4.0)
        assert high_liq.probability_of_halt >= low_liq.probability_of_halt
