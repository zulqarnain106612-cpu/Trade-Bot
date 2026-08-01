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


def test_compute_annualized_basis_pct_rejects_nonpositive_perp() -> None:
    with pytest.raises(ValueError, match="perp_price"):
        compute_annualized_basis_pct(100.0, 0.0)


def test_compute_annualized_basis_pct_rejects_nonpositive_horizon() -> None:
    with pytest.raises(ValueError, match="days_to_normalization"):
        compute_annualized_basis_pct(100.0, 101.0, days_to_normalization=0.0)


def test_annualized_basis_scales_inversely_with_horizon() -> None:
    one_day = compute_annualized_basis_pct(100.0, 100.5, days_to_normalization=1.0)
    seven_day = compute_annualized_basis_pct(100.0, 100.5, days_to_normalization=7.0)
    assert one_day == pytest.approx(seven_day * 7.0)


def test_slow_normalizing_basis_is_a_weaker_signal() -> None:
    """The same raw gap must not earn full confidence over a longer horizon."""
    strat = BasisTradeStrategy()
    fast = strat.generate_signal(BasisTradeContext(spot_price=100.0, perp_price=100.05))
    slow = strat.generate_signal(
        BasisTradeContext(
            spot_price=100.0,
            perp_price=100.05,
            days_to_perp_funding_normalization=30.0,
        )
    )
    assert fast.confidence > slow.confidence


def test_long_horizon_can_fall_below_the_entry_threshold() -> None:
    strat = BasisTradeStrategy()
    ctx = BasisTradeContext(
        spot_price=100.0,
        perp_price=100.05,
        days_to_perp_funding_normalization=365.0,
    )
    assert strat.generate_signal(ctx).direction == 0


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
