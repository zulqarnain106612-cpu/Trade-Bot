"""Tests for src/tuning/stress_simulator.py — portfolio stress testing."""

from __future__ import annotations

from src.tuning.stress_simulator import (
    KNOWN_CRISIS_SCENARIOS,
    StressTestResult,
    run_all_known_scenarios,
    run_stress_scenario,
)


def test_empty_allocation_returns_zero_drawdown() -> None:
    result = run_stress_scenario({}, {}, "test")
    assert result.simulated_max_drawdown_pct == 0.0
    assert result.simulated_final_return_pct == 0.0
    assert not result.breaches_capital_floor


def test_zero_weight_strategies_ignored() -> None:
    allocation = {"strat_a": 0.0}
    result = run_stress_scenario(allocation, {"strat_a": [-0.5, -0.5]}, "test")
    assert result.simulated_max_drawdown_pct == 0.0


def test_no_returns_for_strategy_returns_zero() -> None:
    allocation = {"strat_a": 1.0}
    result = run_stress_scenario(allocation, {}, "test")
    assert result.simulated_max_drawdown_pct == 0.0


def test_single_strategy_crash_scenario() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [-0.3, -0.1]}  # big crash
    result = run_stress_scenario(allocation, returns, "crash")
    assert result.simulated_max_drawdown_pct > 0.0
    assert result.simulated_final_return_pct < 0.0
    assert result.scenario_name == "crash"


def test_capital_floor_breach_detected() -> None:
    allocation = {"strat_a": 1.0}
    # Returns that cause >30% drawdown
    returns = {"strat_a": [-0.20, -0.15, -0.10]}
    result = run_stress_scenario(allocation, returns, "bad", capital_preservation_floor_pct=0.30)
    assert result.breaches_capital_floor


def test_capital_floor_not_breached_for_mild_loss() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [-0.05, -0.03]}  # mild loss, well below 30% floor
    result = run_stress_scenario(allocation, returns, "mild", capital_preservation_floor_pct=0.30)
    assert not result.breaches_capital_floor


def test_positive_returns_no_drawdown() -> None:
    allocation = {"strat_a": 1.0}
    returns = {"strat_a": [0.05, 0.03, 0.02]}
    result = run_stress_scenario(allocation, returns, "bull")
    assert result.simulated_max_drawdown_pct == 0.0
    assert result.simulated_final_return_pct > 0.0


def test_weighted_portfolio_combines_strategies() -> None:
    allocation = {"long_strat": 0.5, "short_strat": 0.5}
    returns = {
        "long_strat": [-0.30, -0.10],
        "short_strat": [0.30, 0.10],  # hedges the crash
    }
    result = run_stress_scenario(allocation, returns, "hedged")
    # Net portfolio return ≈ 0 — drawdown small
    assert result.simulated_max_drawdown_pct < 5.0


def test_missing_strategy_return_treated_as_zero_for_that_step() -> None:
    # strat_b has only 1 return vs strat_a's 3 — gap treated as 0
    allocation = {"strat_a": 0.5, "strat_b": 0.5}
    returns = {
        "strat_a": [-0.10, -0.05, -0.02],
        "strat_b": [-0.10],  # only one period
    }
    result = run_stress_scenario(allocation, returns, "partial")
    assert result.simulated_max_drawdown_pct > 0.0


def test_result_is_dataclass() -> None:
    allocation = {"s": 1.0}
    result = run_stress_scenario(allocation, {"s": [-0.10]}, "test")
    assert isinstance(result, StressTestResult)
    assert isinstance(result.simulated_max_drawdown_pct, float)
    assert isinstance(result.breaches_capital_floor, bool)


def test_known_crisis_scenarios_constant_not_empty() -> None:
    assert len(KNOWN_CRISIS_SCENARIOS) > 0
    for name, returns in KNOWN_CRISIS_SCENARIOS.items():
        assert isinstance(name, str)
        assert len(returns) > 0


def test_run_all_known_scenarios_returns_list() -> None:
    allocation = {"strat_a": 1.0}
    # Provide data for one known scenario
    first_scenario = next(iter(KNOWN_CRISIS_SCENARIOS))
    returns_by_scenario = {first_scenario: {"strat_a": KNOWN_CRISIS_SCENARIOS[first_scenario]}}
    results = run_all_known_scenarios(allocation, returns_by_scenario)
    assert len(results) == 1
    assert results[0].scenario_name == first_scenario


def test_run_all_known_scenarios_skips_missing_data() -> None:
    allocation = {"strat_a": 1.0}
    # No scenario data provided
    results = run_all_known_scenarios(allocation, {})
    assert results == []


def test_run_all_known_scenarios_with_all_data() -> None:
    allocation = {"strat_a": 1.0}
    returns_by_scenario = {
        name: {"strat_a": returns} for name, returns in KNOWN_CRISIS_SCENARIOS.items()
    }
    results = run_all_known_scenarios(allocation, returns_by_scenario)
    assert len(results) == len(KNOWN_CRISIS_SCENARIOS)


def test_ftx_collapse_scenario_causes_significant_drawdown() -> None:
    allocation = {"strat_a": 1.0}
    returns_by_scenario = {
        "2022_ftx_collapse": {"strat_a": KNOWN_CRISIS_SCENARIOS["2022_ftx_collapse"]}
    }
    results = run_all_known_scenarios(allocation, returns_by_scenario)
    assert len(results) == 1
    assert results[0].simulated_max_drawdown_pct > 0.0
