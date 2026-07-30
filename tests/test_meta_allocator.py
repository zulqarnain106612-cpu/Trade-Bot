"""Tests for the v9 self-optimizing meta-allocator."""

from __future__ import annotations

import pytest

from src.tuning.meta_allocator import (
    StrategyPerformance,
    compute_target_allocation,
    rate_limit_allocation_shift,
)


def test_empty_performances_returns_empty() -> None:
    assert compute_target_allocation([]) == {}


def test_disabled_strategies_get_zero() -> None:
    perfs = [StrategyPerformance("a", 1.0, enabled=False), StrategyPerformance("b", 1.0, True)]
    allocation = compute_target_allocation(perfs)
    assert allocation["a"] == 0.0
    assert allocation["b"] > 0.0


def test_all_disabled_returns_all_zero() -> None:
    perfs = [StrategyPerformance("a", 1.0, False), StrategyPerformance("b", 2.0, False)]
    allocation = compute_target_allocation(perfs)
    assert allocation == {"a": 0.0, "b": 0.0}


def test_higher_sharpe_gets_more_allocation() -> None:
    perfs = [StrategyPerformance("a", 2.0, True), StrategyPerformance("b", 0.5, True)]
    allocation = compute_target_allocation(perfs)
    assert allocation["a"] > allocation["b"]


def test_allocations_sum_to_one_among_enabled() -> None:
    perfs = [StrategyPerformance("a", 1.0, True), StrategyPerformance("b", 1.5, True)]
    allocation = compute_target_allocation(perfs)
    assert sum(allocation.values()) == pytest.approx(1.0)


def test_equal_sharpe_gives_equal_allocation() -> None:
    perfs = [StrategyPerformance("a", 1.0, True), StrategyPerformance("b", 1.0, True)]
    allocation = compute_target_allocation(perfs)
    assert allocation["a"] == pytest.approx(allocation["b"])


def test_rate_limit_caps_shift_per_step() -> None:
    current = {"a": 0.5, "b": 0.5}
    target = {"a": 1.0, "b": 0.0}
    result = rate_limit_allocation_shift(current, target, max_shift_per_step=0.10)
    assert result["a"] == pytest.approx(0.6)
    assert result["b"] == pytest.approx(0.4)


def test_rate_limit_no_shift_needed_when_already_at_target() -> None:
    current = {"a": 0.5, "b": 0.5}
    result = rate_limit_allocation_shift(current, current, max_shift_per_step=0.10)
    assert result == current


def test_rate_limit_rejects_invalid_max_shift() -> None:
    with pytest.raises(ValueError, match="max_shift_per_step"):
        rate_limit_allocation_shift({}, {}, max_shift_per_step=0.0)
    with pytest.raises(ValueError, match="max_shift_per_step"):
        rate_limit_allocation_shift({}, {}, max_shift_per_step=1.5)


def test_rate_limit_handles_new_strategy_not_in_current() -> None:
    current = {"a": 1.0}
    target = {"a": 0.9, "b": 0.1}
    result = rate_limit_allocation_shift(current, target, max_shift_per_step=0.05)
    assert result["b"] == pytest.approx(0.05)
    assert result["a"] == pytest.approx(0.95)


def test_softmax_weights_empty_returns_empty() -> None:
    from src.tuning.meta_allocator import _softmax_weights

    assert _softmax_weights([], temperature=1.0) == []
