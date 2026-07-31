"""
Tests for Orchestrator._publish_signal_to_registry.

The signal engine is async and stateful, so SignalEngineStrategy cannot
call it — the orchestrator hands each tick's SignalResult over. Without
that hand-off the incumbent strategy reads as permanently flat to capital
allocation and attribution.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.engine.orchestrator import Orchestrator
from src.engine.signal_engine import SignalResult
from src.strategies.registry import StrategyRegistry
from src.strategies.signal_engine_adapter import (
    STRATEGY_ID_SIGNAL_ENGINE,
    SignalEngineStrategy,
)


def _orchestrator() -> Orchestrator:
    """Orchestrator without __init__ — only the publish path is under test."""
    import structlog

    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    return orch


def _result(*, tradeable: bool = True, p_bet: float = 0.7, direction: int = 1) -> SignalResult:
    return SignalResult(
        tradeable=tradeable,
        direction=direction,
        p_long=0.7,
        p_bet=p_bet,
        kelly_result=None,
        regime=None,
        gate_result=None,
        skip_reason="",
    )


def _patch_registry(monkeypatch, registry: StrategyRegistry) -> None:
    import src.engine.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)


def test_publishes_result_to_registered_adapter(monkeypatch):
    registry = StrategyRegistry()
    adapter = SignalEngineStrategy()
    registry.register(adapter)
    _patch_registry(monkeypatch, registry)

    result = _result(p_bet=0.9, direction=1)
    _orchestrator()._publish_signal_to_registry(result)

    signal = adapter.generate_signal(object())
    assert signal.direction == 1
    assert signal.confidence == pytest.approx(0.9)


def test_untradeable_result_reads_back_as_flat(monkeypatch):
    registry = StrategyRegistry()
    adapter = SignalEngineStrategy()
    registry.register(adapter)
    _patch_registry(monkeypatch, registry)

    _orchestrator()._publish_signal_to_registry(_result(tradeable=False))

    signal = adapter.generate_signal(object())
    assert signal.direction == 0
    assert signal.confidence == pytest.approx(0.0)


def test_no_op_when_strategy_not_registered(monkeypatch):
    # STRATEGY_SIGNAL_ENGINE_ENABLED=false — publishing must not raise.
    registry = StrategyRegistry()
    _patch_registry(monkeypatch, registry)

    _orchestrator()._publish_signal_to_registry(_result())

    assert registry.get(STRATEGY_ID_SIGNAL_ENGINE) is None


def test_ignores_foreign_implementation_under_the_same_id(monkeypatch):
    # The registry is keyed by strategy_id, not type, so a different class
    # can occupy the incumbent's id. submit_result() must not be called on it.
    foreign = MagicMock()
    foreign.strategy_id = STRATEGY_ID_SIGNAL_ENGINE
    foreign.required_capital_fraction = MagicMock(return_value=1.0)
    foreign.generate_signal = MagicMock()
    registry = StrategyRegistry()
    registry.register(foreign)
    _patch_registry(monkeypatch, registry)

    _orchestrator()._publish_signal_to_registry(_result())

    foreign.submit_result.assert_not_called()


def test_registry_failure_never_reaches_the_trade_path(monkeypatch):
    import src.engine.orchestrator as orch_mod

    def _boom() -> StrategyRegistry:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(orch_mod, "get_default_registry", _boom)

    # Must swallow: this is observability plumbing, not a trading gate.
    _orchestrator()._publish_signal_to_registry(_result())


def test_repeated_publish_keeps_only_the_latest_result(monkeypatch):
    registry = StrategyRegistry()
    adapter = SignalEngineStrategy()
    registry.register(adapter)
    _patch_registry(monkeypatch, registry)

    orch = _orchestrator()
    orch._publish_signal_to_registry(_result(direction=1, p_bet=0.9))
    orch._publish_signal_to_registry(_result(direction=0, p_bet=0.6))  # 0 = short

    signal = adapter.generate_signal(object())
    assert signal.direction == -1
    assert signal.confidence == pytest.approx(0.6)
