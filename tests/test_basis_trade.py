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


def test_default_horizon_reproduces_the_historical_scale() -> None:
    # The horizon parameter must not change what this function has always
    # returned; it only makes the assumption explicit.
    assert compute_annualized_basis_pct(100.0, 100.05) == pytest.approx(18.25, abs=1e-6)


def test_horizon_divides_the_annualization() -> None:
    raw_gap_pct = 0.05
    assert compute_annualized_basis_pct(100.0, 100.05, 365.0) == pytest.approx(
        raw_gap_pct, abs=1e-9
    )
    assert compute_annualized_basis_pct(100.0, 100.05, 7.0) == pytest.approx(
        raw_gap_pct * 365.0 / 7.0, abs=1e-9
    )


def test_a_wider_horizon_shrinks_the_signal() -> None:
    near = compute_annualized_basis_pct(100.0, 100.05, 1.0)
    far = compute_annualized_basis_pct(100.0, 100.05, 30.0)
    assert 0.0 < far < near


def test_non_positive_horizon_is_rejected() -> None:
    # Not "instant convergence" — a division by zero wearing a plausible name.
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError, match="days_to_convergence"):
            compute_annualized_basis_pct(100.0, 100.05, bad)


def test_default_horizon_makes_a_routine_perp_premium_clear_the_entry_gate() -> None:
    # Documents the calibration rather than asserting it is correct: at the
    # 1-day default a 1.4bp gap already exceeds the 5% entry threshold.
    from src.strategies.basis_trade import _MIN_ANNUALIZED_BASIS_PCT

    assert compute_annualized_basis_pct(100.0, 100.0137) > _MIN_ANNUALIZED_BASIS_PCT


def test_strategy_uses_the_context_horizon() -> None:
    from src.strategies.basis_trade import BasisTradeContext, BasisTradeStrategy

    strategy = BasisTradeStrategy(0.10)
    near = strategy.generate_signal(
        BasisTradeContext(spot_price=100.0, perp_price=100.05)
    )
    far = strategy.generate_signal(
        BasisTradeContext(
            spot_price=100.0,
            perp_price=100.05,
            days_to_perp_funding_normalization=365.0,
        )
    )
    # Same prices, different stated horizon: the near horizon signals, the
    # year-long one falls below the entry threshold.
    assert near.direction == -1
    assert far.direction == 0
