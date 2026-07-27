"""Tests for the funding-rate carry strategy (v2 Sub-task 2, family 2)."""

from __future__ import annotations

import pytest

from src.strategies.funding_carry import FundingCarryStrategy, FundingContext
from src.strategies.registry import StrategyRegistry


def test_rejects_non_fundingcontext_bar() -> None:
    strat = FundingCarryStrategy()
    with pytest.raises(TypeError, match="FundingContext"):
        strat.generate_signal(bar=None)


def test_flat_when_rate_and_zscore_small() -> None:
    strat = FundingCarryStrategy()
    ctx = FundingContext(funding_rate_pct=0.001, funding_zscore=0.2)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0
    assert sig.confidence == 0.0


def test_flat_when_zscore_stretched_but_rate_negligible() -> None:
    strat = FundingCarryStrategy()
    ctx = FundingContext(funding_rate_pct=0.001, funding_zscore=3.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_shorts_perp_on_stretched_positive_funding() -> None:
    strat = FundingCarryStrategy()
    ctx = FundingContext(funding_rate_pct=0.05, funding_zscore=2.5)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1
    assert sig.confidence > 0.0
    assert sig.regime_fit == pytest.approx(0.6)


def test_longs_perp_on_stretched_negative_funding() -> None:
    strat = FundingCarryStrategy()
    ctx = FundingContext(funding_rate_pct=-0.05, funding_zscore=-2.5)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1


def test_confidence_capped_at_one() -> None:
    strat = FundingCarryStrategy()
    ctx = FundingContext(funding_rate_pct=0.5, funding_zscore=10.0)
    sig = strat.generate_signal(ctx)
    assert sig.confidence == 1.0


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(FundingCarryStrategy())
    assert "funding_carry_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        FundingCarryStrategy(max_capital_fraction=1.5)
    with pytest.raises(ValueError, match="max_capital_fraction"):
        FundingCarryStrategy(max_capital_fraction=0.0)
