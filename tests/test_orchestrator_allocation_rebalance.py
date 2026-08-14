"""
Tests for Orchestrator._allocation_rebalance_loop.

performance_weighted_allocate is stateless — it answers "what split is
optimal right now?". Applied directly, that lets one noisy attribution
window reallocate the whole book. This loop is the thing that keeps the
incumbent allocation and steps toward the target at a bounded rate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
import structlog

from src.config import StrategyPortfolioSettings
from src.engine.orchestrator import Orchestrator
from src.strategies.capital_allocator import AllocationResult
from src.tuning.meta_allocator import reset_allocation_controller


@pytest.fixture(autouse=True)
def _clean_controller() -> Iterator[None]:
    reset_allocation_controller()
    yield
    reset_allocation_controller()


def _orchestrator(*, max_shift: float = 0.10, interval_s: int = 60) -> Orchestrator:
    """Orchestrator without __init__ — only the rebalance loop is under test."""
    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    orch._running = True
    orch._cfg = MagicMock()
    orch._cfg.strategy_portfolio = StrategyPortfolioSettings(
        max_allocation_shift_per_step=max_shift,
        allocation_rebalance_interval_s=interval_s,
    )
    return orch


def _strategy(strategy_id: str) -> MagicMock:
    s = MagicMock()
    s.strategy_id = strategy_id
    s.required_capital_fraction.return_value = 1.0
    return s


def _patch(
    monkeypatch,
    orch: Orchestrator,
    *,
    strategies: list[MagicMock],
    targets: list[dict[str, float]],
) -> None:
    """
    Patch the loop's collaborators.

    `targets` is consumed one per rebalance; the loop stops once the last one
    has been handed out, so a test's iteration count is just its target count.
    """
    import src.engine.orchestrator as orch_mod

    registry = MagicMock()
    registry.all.return_value = strategies
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)

    manager = MagicMock()
    manager.enabled_ids.side_effect = lambda ids: set(ids)
    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: manager)

    pending = list(targets)

    def _allocate(_strategies, _enabled_ids):
        fractions = pending.pop(0)
        if not pending:
            orch._running = False
        return AllocationResult(fractions=fractions, method="performance_weighted")

    monkeypatch.setattr(orch_mod, "performance_weighted_allocate", _allocate)


async def _run(orch: Orchestrator, monkeypatch, *, max_sleeps: int = 20) -> None:
    """
    Drive the loop with the cadence removed.

    max_sleeps is a runaway guard, not the stop condition — a loop that never
    reaches its last target should fail the test rather than hang the suite.
    """
    import src.engine.orchestrator as orch_mod

    calls = {"n": 0}

    async def _sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] > max_sleeps:
            raise asyncio.CancelledError

    monkeypatch.setattr(orch_mod.asyncio, "sleep", _sleep)
    await orch._allocation_rebalance_loop()
    assert calls["n"] <= max_sleeps, "rebalance loop did not terminate"


@pytest.mark.asyncio
async def test_first_rebalance_adopts_the_target(monkeypatch):
    from src.tuning.meta_allocator import get_allocation_controller

    orch = _orchestrator()
    _patch(
        monkeypatch,
        orch,
        strategies=[_strategy("a"), _strategy("b")],
        targets=[{"a": 0.8, "b": 0.2}],
    )
    await _run(orch, monkeypatch)

    assert get_allocation_controller().applied() == {"a": 0.8, "b": 0.2}


@pytest.mark.asyncio
async def test_second_rebalance_is_rate_limited(monkeypatch):
    from src.tuning.meta_allocator import get_allocation_controller

    orch = _orchestrator(max_shift=0.10)
    _patch(
        monkeypatch,
        orch,
        strategies=[_strategy("a"), _strategy("b")],
        targets=[{"a": 0.5, "b": 0.5}, {"a": 1.0, "b": 0.0}],
    )
    await _run(orch, monkeypatch)

    applied = get_allocation_controller().applied()
    assert applied["a"] == pytest.approx(0.6)
    assert applied["b"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_empty_registry_leaves_allocation_untouched(monkeypatch):
    """No registered strategies means nothing to weight — and nothing to apply."""
    import src.engine.orchestrator as orch_mod
    from src.tuning.meta_allocator import get_allocation_controller

    orch = _orchestrator()
    registry = MagicMock()

    def _all():
        orch._running = False
        return []

    registry.all.side_effect = _all
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(
        orch_mod,
        "performance_weighted_allocate",
        MagicMock(side_effect=AssertionError("allocator called with an empty registry")),
    )
    await _run(orch, monkeypatch)

    assert get_allocation_controller().applied() == {}


@pytest.mark.asyncio
async def test_allocator_failure_does_not_kill_the_loop(monkeypatch):
    """A bad rebalance must not silently retire the loop for the process."""
    import src.engine.orchestrator as orch_mod
    from src.tuning.meta_allocator import get_allocation_controller

    orch = _orchestrator()
    _patch(monkeypatch, orch, strategies=[_strategy("a")], targets=[{"a": 1.0}])

    boom = {"raised": False}
    stubbed = orch_mod.performance_weighted_allocate

    def _allocate(strategies, enabled_ids):
        if not boom["raised"]:
            boom["raised"] = True
            raise RuntimeError("attribution exploded")
        return stubbed(strategies, enabled_ids)

    monkeypatch.setattr(orch_mod, "performance_weighted_allocate", _allocate)
    await _run(orch, monkeypatch)

    assert boom["raised"]
    assert get_allocation_controller().applied() == {"a": 1.0}


@pytest.mark.asyncio
async def test_cancellation_exits_cleanly(monkeypatch):
    import src.engine.orchestrator as orch_mod

    orch = _orchestrator()
    _patch(monkeypatch, orch, strategies=[_strategy("a")], targets=[{"a": 1.0}])

    async def _sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(orch_mod.asyncio, "sleep", _sleep)
    await orch._allocation_rebalance_loop()
