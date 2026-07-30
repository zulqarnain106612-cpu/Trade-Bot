"""Tests for SignalEngineStrategy adapter."""

from __future__ import annotations

import pytest

from src.engine.signal_engine import SignalResult
from src.strategies.signal_engine_adapter import STRATEGY_ID_SIGNAL_ENGINE, SignalEngineStrategy


def _make_result(
    tradeable: bool,
    direction: int = 1,
    p_bet: float = 0.7,
    gate_passed: bool | None = True,
) -> SignalResult:
    class _Gate:
        def __init__(self, passed: bool) -> None:
            self.passed = passed

    gate = _Gate(gate_passed) if gate_passed is not None else None  # type: ignore[arg-type]
    return SignalResult(
        tradeable=tradeable,
        direction=direction,
        p_long=0.6,
        p_bet=p_bet,
        kelly_result=None,
        regime=None,
        gate_result=gate,  # type: ignore[arg-type]
        skip_reason="" if tradeable else "gates_failed",
        regime_agreement_scalar=1.0,
    )


def test_strategy_id_constant() -> None:
    assert STRATEGY_ID_SIGNAL_ENGINE == "signal_engine_v1"
    assert SignalEngineStrategy.strategy_id == "signal_engine_v1"


def test_default_capital_fraction() -> None:
    s = SignalEngineStrategy()
    assert s.required_capital_fraction() == 1.0


def test_custom_capital_fraction() -> None:
    s = SignalEngineStrategy(max_capital_fraction=0.3)
    assert s.required_capital_fraction() == 0.3


def test_invalid_capital_fraction_raises() -> None:
    with pytest.raises(ValueError):
        SignalEngineStrategy(max_capital_fraction=0.0)
    with pytest.raises(ValueError):
        SignalEngineStrategy(max_capital_fraction=1.1)


def test_no_result_returns_flat_signal() -> None:
    s = SignalEngineStrategy()
    sig = s.generate_signal(bar=None)
    assert sig.direction == 0
    assert sig.confidence == 0.0
    assert sig.regime_fit == 0.0


def test_not_tradeable_returns_flat_signal() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=False))
    sig = s.generate_signal(bar=None)
    assert sig.direction == 0


def test_tradeable_long_signal() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=True, direction=1, p_bet=0.8, gate_passed=True))
    sig = s.generate_signal(bar=None)
    assert sig.direction == 1
    assert sig.confidence == pytest.approx(0.8)
    assert sig.regime_fit == 1.0


def test_tradeable_short_maps_to_minus_one() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=True, direction=-1, p_bet=0.5, gate_passed=True))
    sig = s.generate_signal(bar=None)
    assert sig.direction == -1


def test_failed_gate_gives_zero_regime_fit() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=True, direction=1, p_bet=0.6, gate_passed=False))
    sig = s.generate_signal(bar=None)
    assert sig.regime_fit == 0.0


def test_none_gate_result_gives_one_regime_fit() -> None:
    s = SignalEngineStrategy()
    r = _make_result(tradeable=True, direction=1, p_bet=0.5, gate_passed=None)
    s.submit_result(r)
    sig = s.generate_signal(bar=None)
    assert sig.regime_fit == 1.0


def test_p_bet_clipped_to_zero_one() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=True, direction=1, p_bet=5.0, gate_passed=True))
    sig = s.generate_signal(bar=None)
    assert sig.confidence == pytest.approx(1.0)

    s.submit_result(_make_result(tradeable=True, direction=1, p_bet=-2.0, gate_passed=True))
    sig = s.generate_signal(bar=None)
    assert sig.confidence == pytest.approx(0.0)


def test_submit_result_overrides_previous() -> None:
    s = SignalEngineStrategy()
    s.submit_result(_make_result(tradeable=True, direction=1, p_bet=0.9, gate_passed=True))
    s.submit_result(_make_result(tradeable=False))
    sig = s.generate_signal(bar=None)
    assert sig.direction == 0
