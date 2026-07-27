"""Tests for the v2 equal-weight capital allocator."""

from __future__ import annotations

import pytest

from src.strategies.capital_allocator import equal_weight_allocate
from src.strategies.registry import Signal


class _Strat:
    def __init__(self, strategy_id: str, cap: float) -> None:
        self.strategy_id = strategy_id
        self._cap = cap

    def generate_signal(self, bar: object) -> Signal:
        return Signal(0, 0.0, 0.0)

    def required_capital_fraction(self) -> float:
        return self._cap


def test_no_enabled_strategies_all_zero() -> None:
    strategies = (_Strat("a", 0.5), _Strat("b", 0.5))
    result = equal_weight_allocate(strategies, enabled_ids=set())
    assert result.fractions == {"a": 0.0, "b": 0.0}
    assert result.total() == 0.0


def test_equal_split_when_uncapped() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    assert result.fractions["a"] == pytest.approx(0.5)
    assert result.fractions["b"] == pytest.approx(0.5)
    assert result.total() == pytest.approx(1.0)


def test_disabled_strategy_gets_zero_others_still_allocated() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0), _Strat("c", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    assert result.fractions["c"] == 0.0
    assert result.fractions["a"] == pytest.approx(0.5)
    assert result.fractions["b"] == pytest.approx(0.5)


def test_per_strategy_cap_respected() -> None:
    strategies = (_Strat("a", 0.1), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    # a's equal share (0.5) is capped to its own 0.1 ceiling before renorm.
    assert result.fractions["a"] <= 0.1 + 1e-9
    assert result.total() <= 1.0 + 1e-9


def test_total_never_exceeds_one() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0), _Strat("c", 1.0), _Strat("d", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b", "c", "d"})
    assert result.total() <= 1.0 + 1e-9


def test_single_enabled_strategy_gets_full_capped_allocation() -> None:
    strategies = (_Strat("a", 0.3), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a"})
    assert result.fractions["a"] == pytest.approx(0.3)
    assert result.fractions["b"] == 0.0
