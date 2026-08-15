"""Contract tests for the v2 strategy registry (Sub-task 1)."""

from __future__ import annotations

import pytest

from src.strategies.registry import (
    DuplicateStrategyError,
    Signal,
    StrategyRegistry,
    get_default_registry,
)
from src.strategies.signal_engine_adapter import (
    STRATEGY_ID_SIGNAL_ENGINE,
    SignalEngineStrategy,
)


class _StubStrategy:
    def __init__(self, strategy_id: str = "stub", capital_fraction: float = 0.1) -> None:
        self.strategy_id = strategy_id
        self._capital_fraction = capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        return Signal(direction=1, confidence=0.6, regime_fit=0.8)

    def required_capital_fraction(self) -> float:
        return self._capital_fraction


class _MalformedStrategy:
    """Missing generate_signal / required_capital_fraction."""

    strategy_id = "malformed"


def test_signal_validates_direction() -> None:
    with pytest.raises(ValueError, match="direction"):
        Signal(direction=2, confidence=0.5, regime_fit=0.5)


def test_signal_validates_confidence_bounds() -> None:
    with pytest.raises(ValueError, match="confidence"):
        Signal(direction=1, confidence=1.5, regime_fit=0.5)


def test_signal_validates_regime_fit_bounds() -> None:
    with pytest.raises(ValueError, match="regime_fit"):
        Signal(direction=1, confidence=0.5, regime_fit=-0.1)


def test_signal_accepts_valid_bounds() -> None:
    sig = Signal(direction=-1, confidence=0.0, regime_fit=1.0)
    assert sig.direction == -1
    assert sig.confidence == 0.0
    assert sig.regime_fit == 1.0


def test_register_and_get() -> None:
    registry = StrategyRegistry()
    strat = _StubStrategy()
    registry.register(strat)
    assert len(registry) == 1
    assert "stub" in registry
    assert registry.get("stub") is strat
    assert registry.all() == (strat,)


def test_duplicate_strategy_id_rejected() -> None:
    registry = StrategyRegistry()
    registry.register(_StubStrategy(strategy_id="dup"))
    with pytest.raises(DuplicateStrategyError):
        registry.register(_StubStrategy(strategy_id="dup"))


def test_empty_strategy_id_rejected() -> None:
    registry = StrategyRegistry()
    with pytest.raises(ValueError, match="strategy_id"):
        registry.register(_StubStrategy(strategy_id=""))


def test_malformed_strategy_rejected() -> None:
    registry = StrategyRegistry()
    with pytest.raises(TypeError):
        registry.register(_MalformedStrategy())  # type: ignore[arg-type]


def test_invalid_capital_fraction_rejected() -> None:
    registry = StrategyRegistry()
    with pytest.raises(ValueError, match="required_capital_fraction"):
        registry.register(_StubStrategy(capital_fraction=1.5))
    with pytest.raises(ValueError, match="required_capital_fraction"):
        registry.register(_StubStrategy(strategy_id="zero", capital_fraction=0.0))


def test_unregister() -> None:
    registry = StrategyRegistry()
    registry.register(_StubStrategy())
    registry.unregister("stub")
    assert len(registry) == 0
    assert registry.get("stub") is None


def test_get_default_registry_singleton() -> None:
    reg1 = get_default_registry()
    reg2 = get_default_registry()
    assert reg1 is reg2


def test_signal_engine_adapter_not_tradeable_returns_flat_signal() -> None:
    adapter = SignalEngineStrategy()
    sig = adapter.generate_signal(bar=None)
    assert sig == Signal(direction=0, confidence=0.0, regime_fit=0.0)


def test_signal_engine_adapter_no_result_yet_returns_flat_signal() -> None:
    adapter = SignalEngineStrategy()
    assert adapter.strategy_id == STRATEGY_ID_SIGNAL_ENGINE
    sig = adapter.generate_signal(bar=object())
    assert sig.direction == 0


def test_signal_engine_adapter_tradeable_result_maps_to_signal() -> None:
    from src.risk.gates import GateResult, GateStatus

    adapter = SignalEngineStrategy()

    class _FakeResult:
        tradeable = True
        direction = 1
        p_long = 0.7
        p_bet = 0.65
        kelly_result = None
        regime = None
        gate_result = GateResult(status=GateStatus.PASS, passed=True, reason="ok", details={})
        skip_reason = ""

    adapter.submit_result(_FakeResult())  # type: ignore[arg-type]
    sig = adapter.generate_signal(bar=None)
    assert sig.direction == 1
    assert sig.confidence == pytest.approx(0.65)
    assert sig.regime_fit == 1.0


def test_signal_engine_adapter_short_direction_maps_to_minus_one() -> None:
    from src.risk.gates import GateResult, GateStatus

    adapter = SignalEngineStrategy()

    class _FakeResult:
        tradeable = True
        direction = 0
        p_long = 0.3
        p_bet = 0.55
        kelly_result = None
        regime = None
        gate_result = GateResult(status=GateStatus.PASS, passed=True, reason="ok", details={})
        skip_reason = ""

    adapter.submit_result(_FakeResult())  # type: ignore[arg-type]
    sig = adapter.generate_signal(bar=None)
    assert sig.direction == -1


def test_signal_engine_adapter_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        SignalEngineStrategy(max_capital_fraction=0.0)
    with pytest.raises(ValueError, match="max_capital_fraction"):
        SignalEngineStrategy(max_capital_fraction=1.1)


def test_signal_engine_adapter_registers_cleanly() -> None:
    registry = StrategyRegistry()
    registry.register(SignalEngineStrategy())
    assert STRATEGY_ID_SIGNAL_ENGINE in registry
