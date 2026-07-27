"""Tests for the v5 spot-perp basis trade strategy."""

from __future__ import annotations

import pytest

from src.strategies.basis_trade import (
    BasisTradeContext,
    BasisTradeStrategy,
    compute_annualized_basis_pct,
)
from src.strategies.registry import StrategyRegistry


def test_compute_annualized_basis_pct_positive() -> None:
    pct = compute_annualized_basis_pct(spot_price=100.0, perp_price=100.05)
    assert pct > 0


def test_compute_annualized_basis_pct_rejects_nonpositive_spot() -> None:
    with pytest.raises(ValueError, match="spot_price"):
        compute_annualized_basis_pct(0.0, 100.0)


def test_rejects_non_basistradecontext_bar() -> None:
    strat = BasisTradeStrategy()
    with pytest.raises(TypeError, match="BasisTradeContext"):
        strat.generate_signal(bar=None)


def test_flat_when_basis_small() -> None:
    strat = BasisTradeStrategy()
    ctx = BasisTradeContext(spot_price=100.0, perp_price=100.001)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_shorts_perp_on_contango() -> None:
    strat = BasisTradeStrategy()
    ctx = BasisTradeContext(spot_price=100.0, perp_price=100.5)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1
    assert sig.confidence > 0.0


def test_longs_perp_on_backwardation() -> None:
    strat = BasisTradeStrategy()
    ctx = BasisTradeContext(spot_price=100.0, perp_price=99.5)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(BasisTradeStrategy())
    assert "basis_trade_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        BasisTradeStrategy(max_capital_fraction=0.0)
