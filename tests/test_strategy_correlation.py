"""Tests for the strategy-level correlation layer (v2 Sub-task 3)."""

from __future__ import annotations

import pytest

from src.risk.strategy_correlation import (
    StrategyCorrelationTracker,
    combined_correlation_scalar,
    get_strategy_correlation,
)


def test_no_data_returns_full_scalar() -> None:
    tracker = StrategyCorrelationTracker()
    scalar = tracker.correlation_scalar("new_strat", ["other_strat"])
    assert scalar == 1.0


def test_uncorrelated_strategies_no_reduction() -> None:
    tracker = StrategyCorrelationTracker(halflife=50)
    import random

    rng = random.Random(1)
    for _ in range(60):
        tracker.push_strategy_returns(
            {
                "strat_a": rng.gauss(0, 1),
                "strat_b": rng.gauss(0, 1),
            }
        )
    scalar = tracker.correlation_scalar("strat_a", ["strat_b"])
    assert scalar == pytest.approx(1.0, abs=0.05)


def test_highly_correlated_strategies_reduce_scalar() -> None:
    tracker = StrategyCorrelationTracker(halflife=50)
    import random

    rng = random.Random(2)
    for _ in range(60):
        base = rng.gauss(0, 1)
        tracker.push_strategy_returns(
            {
                "strat_a": base + rng.gauss(0, 0.01),
                "strat_b": base + rng.gauss(0, 0.01),
            }
        )
    scalar = tracker.correlation_scalar("strat_a", ["strat_b"])
    assert scalar < 1.0


def test_correlation_passthrough_returns_none_without_data() -> None:
    tracker = StrategyCorrelationTracker()
    assert tracker.correlation("strat_a", "strat_b") is None


def test_correlation_passthrough_after_pushed_returns() -> None:
    tracker = StrategyCorrelationTracker(halflife=30)
    for i in range(35):
        tracker.push_strategy_returns({"strat_a": 0.01 * i, "strat_b": 0.01 * i})
    r = tracker.correlation("strat_a", "strat_b")
    assert r is not None
    assert r > 0.5


def test_avg_correlation_with_active_strategies_no_active() -> None:
    tracker = StrategyCorrelationTracker()
    avg = tracker.avg_correlation_with_active_strategies("strat_a", [])
    assert avg == 0.0


def test_avg_correlation_with_active_strategies_with_data() -> None:
    tracker = StrategyCorrelationTracker(halflife=30)
    for i in range(35):
        tracker.push_strategy_returns({"strat_a": 0.01 * i, "strat_b": 0.01 * i})
    avg = tracker.avg_correlation_with_active_strategies("strat_a", ["strat_b"])
    assert avg > 0.5


def test_tracked_strategy_ids_reports_registered_strategies() -> None:
    tracker = StrategyCorrelationTracker(halflife=30)
    tracker.push_strategy_returns({"strat_a": 0.01, "strat_b": -0.005})
    assert set(tracker.tracked_strategy_ids) == {"strat_a", "strat_b"}


def test_correlation_matrix_returns_pairwise_entries() -> None:
    tracker = StrategyCorrelationTracker(halflife=30)
    for i in range(35):
        tracker.push_strategy_returns({"strat_a": 0.01 * i, "strat_b": -0.01 * i})
    matrix = tracker.correlation_matrix()
    assert ("strat_a", "strat_b") in matrix


def test_get_strategy_correlation_singleton() -> None:
    t1 = get_strategy_correlation()
    t2 = get_strategy_correlation()
    assert t1 is t2


def test_combined_scalar_multiplies() -> None:
    assert combined_correlation_scalar(0.8, 0.5) == pytest.approx(0.4)
    assert combined_correlation_scalar(1.0, 1.0) == pytest.approx(1.0)


def test_combined_scalar_rejects_out_of_range_asset_scalar() -> None:
    with pytest.raises(ValueError, match="asset_scalar"):
        combined_correlation_scalar(1.5, 0.5)


def test_combined_scalar_rejects_out_of_range_strategy_scalar() -> None:
    with pytest.raises(ValueError, match="strategy_scalar"):
        combined_correlation_scalar(0.5, -0.1)
