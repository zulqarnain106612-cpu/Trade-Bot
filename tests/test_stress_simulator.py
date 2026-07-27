"""Tests for the v9 historical crash-replay stress simulator."""

from __future__ import annotations

import pytest

from src.tuning.stress_simulator import (
    KNOWN_CRISIS_SCENARIOS,
    run_all_known_scenarios,
    run_stress_scenario,
)


def test_empty_allocation_returns_zero_result() -> None:
    result = run_stress_scenario({}, {}, "test_scenario")
    assert result.simulated_max_drawdown_pct == 0.0
    assert not result.breaches_capital_floor


def test_losing_scenario_produces_drawdown() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [-0.10, -0.10, -0.10]}
    result = run_stress_scenario(allocation, returns, "loss_scenario")
    assert result.simulated_max_drawdown_pct > 0.0
    assert result.simulated_final_return_pct < 0.0


def test_breaches_floor_on_severe_drawdown() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [-0.20, -0.20, -0.20]}
    result = run_stress_scenario(allocation, returns, "severe", capital_preservation_floor_pct=0.30)
    assert result.breaches_capital_floor


def test_does_not_breach_floor_on_mild_drawdown() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [-0.01, 0.005, -0.01]}
    result = run_stress_scenario(allocation, returns, "mild", capital_preservation_floor_pct=0.30)
    assert not result.breaches_capital_floor


def test_diversified_allocation_dampens_single_strategy_loss() -> None:
    allocation = {"strat_a": 0.5, "strat_b": 0.5}
    returns = {"strat_a": [-0.20, -0.20], "strat_b": [0.05, 0.05]}
    result = run_stress_scenario(allocation, returns, "mixed")
    solo_returns = {"strat_a": [-0.20, -0.20]}
    solo_result = run_stress_scenario({"strat_a": 1.0}, solo_returns, "solo")
    assert result.simulated_max_drawdown_pct < solo_result.simulated_max_drawdown_pct


def test_zero_weight_strategies_excluded() -> None:
    allocation = {"strat_a": 1.0, "strat_b": 0.0}
    returns = {"strat_a": [-0.05], "strat_b": [-0.90]}
    result = run_stress_scenario(allocation, returns, "test")
    assert result.simulated_max_drawdown_pct == pytest.approx(5.0, abs=0.1)


def test_run_all_known_scenarios_skips_missing_data() -> None:
    allocation = {"strat_a": 1.0}
    partial_data = {
        next(iter(KNOWN_CRISIS_SCENARIOS)): {"strat_a": [-0.05, -0.03]},
    }
    results = run_all_known_scenarios(allocation, partial_data)
    assert len(results) == 1


def test_run_all_known_scenarios_empty_data_returns_empty() -> None:
    results = run_all_known_scenarios({"strat_a": 1.0}, {})
    assert results == []
