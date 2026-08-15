"""
Historical crash-replay stress simulator — v9 Self-Optimizing Capital
Allocation.

Replays a fixed set of historical crisis-period return sequences against
the *current* proposed allocation, so a reallocation decision (from
meta_allocator.py) can be checked for tail risk before it goes live —
independent of whatever backtest period justified the allocation in the
first place. This is a portfolio-level stress test, not a strategy
backtest.

Authority:
  - Domain Prior: validate signals out-of-sample; in-sample metrics alone
    are not sufficient — extended here to "in-sample allocation choices
    are not sufficient without an out-of-crisis stress check"
"""

from __future__ import annotations

from dataclasses import dataclass


# Illustrative crisis-period daily return sequences (% as decimals) for
# known historical crypto crashes. Real deployments should replace these
# with actual historical OHLCV-derived return series fetched via the
# existing providers; these are stand-ins for known drawdown magnitudes
# so the simulator has a concrete numeric input to run against.
KNOWN_CRISIS_SCENARIOS: dict[str, list[float]] = {
    "2018_crypto_winter": [-0.12, -0.08, -0.15, -0.05, -0.10, -0.07, -0.09],
    "2020_covid_crash": [-0.30, -0.05, 0.10, -0.08, 0.05, -0.03, 0.07],
    "2022_ftx_collapse": [-0.15, -0.10, -0.20, -0.05, -0.03, 0.02, -0.04],
}


@dataclass(frozen=True, slots=True)
class StressTestResult:
    scenario_name: str
    simulated_max_drawdown_pct: float
    simulated_final_return_pct: float
    breaches_capital_floor: bool


def run_stress_scenario(
    allocation: dict[str, float],
    strategy_scenario_returns: dict[str, list[float]],
    scenario_name: str,
    capital_preservation_floor_pct: float = 0.30,
) -> StressTestResult:
    """
    Applies each strategy's allocation weight to its own scenario-specific
    return sequence (different strategies may respond differently to the
    same macro crisis — e.g. a funding-carry strategy may profit while a
    momentum strategy loses), sums to a portfolio return path, and
    computes the resulting drawdown.
    """
    strategy_ids = [sid for sid in allocation if allocation[sid] > 0.0]
    if not strategy_ids:
        return StressTestResult(scenario_name, 0.0, 0.0, False)

    max_len = max((len(strategy_scenario_returns.get(sid, [])) for sid in strategy_ids), default=0)
    if max_len == 0:
        return StressTestResult(scenario_name, 0.0, 0.0, False)

    portfolio_returns: list[float] = []
    for t in range(max_len):
        step_return = 0.0
        for sid in strategy_ids:
            series = strategy_scenario_returns.get(sid, [])
            if t < len(series):
                step_return += allocation[sid] * series[t]
        portfolio_returns.append(step_return)

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for r in portfolio_returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)

    final_return_pct = (equity - 1.0) * 100.0
    return StressTestResult(
        scenario_name=scenario_name,
        simulated_max_drawdown_pct=max_drawdown * 100.0,
        simulated_final_return_pct=final_return_pct,
        breaches_capital_floor=max_drawdown >= capital_preservation_floor_pct,
    )


def run_all_known_scenarios(
    allocation: dict[str, float],
    strategy_scenario_returns_by_scenario: dict[str, dict[str, list[float]]],
    capital_preservation_floor_pct: float = 0.30,
) -> list[StressTestResult]:
    """Runs every scenario in KNOWN_CRISIS_SCENARIOS that has data supplied for it."""
    results: list[StressTestResult] = []
    for scenario_name in KNOWN_CRISIS_SCENARIOS:
        strategy_returns = strategy_scenario_returns_by_scenario.get(scenario_name)
        if strategy_returns is None:
            continue
        results.append(
            run_stress_scenario(
                allocation, strategy_returns, scenario_name, capital_preservation_floor_pct
            )
        )
    return results
