"""Tests for the v5 options carry strategy."""

from __future__ import annotations

import pytest

from src.strategies.options_carry import OptionsCarryContext, OptionsCarryStrategy
from src.strategies.registry import StrategyRegistry


def test_rejects_non_optionscarrycontext_bar() -> None:
    strat = OptionsCarryStrategy()
    with pytest.raises(TypeError, match="OptionsCarryContext"):
        strat.generate_signal(bar=None)


def test_flat_when_iv_not_stretched() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=0.5, holding_direction=1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_flat_when_no_holding_direction() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_covered_call_signal_when_long_and_iv_rich() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1
    assert sig.confidence > 0.0


def test_cash_secured_put_signal_when_flat_and_iv_rich() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=-1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(OptionsCarryStrategy())
    assert "options_carry_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        OptionsCarryStrategy(max_capital_fraction=0.0)
