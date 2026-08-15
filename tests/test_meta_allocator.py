"""Tests for the v9 self-optimizing meta-allocator."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.tuning.meta_allocator import (
    AllocationController,
    StrategyPerformance,
    compute_target_allocation,
    get_allocation_controller,
    rate_limit_allocation_shift,
    reset_allocation_controller,
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


# ---------------------------------------------------------------------------
# AllocationController — the stateful side of the rate limit
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_controller() -> Iterator[None]:
    reset_allocation_controller()
    yield
    reset_allocation_controller()


def test_controller_rejects_invalid_max_shift() -> None:
    with pytest.raises(ValueError, match="max_shift_per_step"):
        AllocationController(max_shift_per_step=0.0)


def test_controller_starts_with_no_incumbent() -> None:
    assert AllocationController().applied() == {}


def test_controller_first_step_adopts_target_outright() -> None:
    controller = AllocationController(max_shift_per_step=0.10)
    applied = controller.step_toward({"a": 0.7, "b": 0.3})
    assert applied == {"a": 0.7, "b": 0.3}


def test_controller_second_step_is_rate_limited() -> None:
    controller = AllocationController(max_shift_per_step=0.10)
    controller.step_toward({"a": 0.5, "b": 0.5})
    applied = controller.step_toward({"a": 1.0, "b": 0.0})
    assert applied["a"] == pytest.approx(0.6)
    assert applied["b"] == pytest.approx(0.4)


def test_controller_converges_over_repeated_steps() -> None:
    controller = AllocationController(max_shift_per_step=0.10)
    controller.step_toward({"a": 0.5, "b": 0.5})
    for _ in range(10):
        applied = controller.step_toward({"a": 1.0, "b": 0.0})
    assert applied["a"] == pytest.approx(1.0)
    assert applied["b"] == pytest.approx(0.0)


def test_controller_applied_is_a_copy() -> None:
    controller = AllocationController()
    controller.step_toward({"a": 1.0})
    controller.applied()["a"] = 99.0
    assert controller.applied()["a"] == pytest.approx(1.0)


def test_controller_reset_drops_incumbent() -> None:
    controller = AllocationController(max_shift_per_step=0.10)
    controller.step_toward({"a": 1.0, "b": 0.0})
    controller.reset()
    # Without the reset this step would be capped at 0.10; after it the
    # controller re-adopts the target outright.
    assert controller.step_toward({"a": 0.0, "b": 1.0}) == {"a": 0.0, "b": 1.0}


def test_get_allocation_controller_is_a_singleton() -> None:
    assert get_allocation_controller(0.20) is get_allocation_controller()


def test_singleton_ignores_a_later_wider_rate_limit() -> None:
    """A second caller must not be able to loosen the live book's rate limit."""
    first = get_allocation_controller(0.05)
    assert get_allocation_controller(0.90).max_shift_per_step == pytest.approx(0.05)
    assert first.max_shift_per_step == pytest.approx(0.05)


def test_reset_allocation_controller_rebuilds_the_singleton() -> None:
    first = get_allocation_controller(0.05)
    reset_allocation_controller()
    assert get_allocation_controller(0.20) is not first
