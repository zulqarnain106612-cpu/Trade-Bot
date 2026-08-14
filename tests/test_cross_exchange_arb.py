"""Tests for the v3 cross-exchange basis arbitrage strategy."""

from __future__ import annotations

import pytest

from src.strategies.cross_exchange_arb import (
    CrossExchangeArbStrategy,
    CrossExchangeContext,
    compute_basis_bps,
)
from src.strategies.registry import StrategyRegistry


def test_compute_basis_bps_positive_spread() -> None:
    bps = compute_basis_bps(price_a=60100.0, price_b=60000.0)
    assert bps == pytest.approx((100.0 / 60000.0) * 10_000.0)


def test_compute_basis_bps_rejects_nonpositive_price_b() -> None:
    with pytest.raises(ValueError, match="price_b"):
        compute_basis_bps(100.0, 0.0)


def test_rejects_non_crossexchangecontext_bar() -> None:
    strat = CrossExchangeArbStrategy()
    with pytest.raises(TypeError, match="CrossExchangeContext"):
        strat.generate_signal(bar=None)


def test_flat_when_spread_below_minimum() -> None:
    strat = CrossExchangeArbStrategy()
    ctx = CrossExchangeContext("binance", 60005.0, "okx", 60000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_shorts_a_when_a_richer_than_b() -> None:
    strat = CrossExchangeArbStrategy()
    ctx = CrossExchangeContext("binance", 60300.0, "okx", 60000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1
    assert sig.confidence > 0.0


def test_longs_a_when_a_cheaper_than_b() -> None:
    strat = CrossExchangeArbStrategy()
    ctx = CrossExchangeContext("binance", 59700.0, "okx", 60000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1


def test_confidence_capped_at_one() -> None:
    strat = CrossExchangeArbStrategy()
    ctx = CrossExchangeContext("binance", 66000.0, "okx", 60000.0)
    sig = strat.generate_signal(ctx)
    assert sig.confidence == 1.0


def test_compute_basis_bps_rejects_nonpositive_price_a() -> None:
    with pytest.raises(ValueError, match="price_a"):
        compute_basis_bps(0.0, 100.0)


def test_rejects_negative_round_trip_cost() -> None:
    with pytest.raises(ValueError, match="round_trip_cost_bps"):
        CrossExchangeArbStrategy(round_trip_cost_bps=-1.0)


def test_flat_when_spread_clears_floor_but_not_the_round_trip_cost() -> None:
    """20bps gross beats the 15bps floor but loses to a 12bps round trip."""
    strat = CrossExchangeArbStrategy()
    ctx = CrossExchangeContext("binance", 60120.0, "okx", 60000.0)
    assert strat.generate_signal(ctx).direction == 0


def test_zero_cost_venue_pair_takes_the_same_spread() -> None:
    ctx = CrossExchangeContext("binance", 60120.0, "okx", 60000.0)
    assert CrossExchangeArbStrategy(round_trip_cost_bps=0.0).generate_signal(ctx).direction == -1


def test_higher_cost_lowers_confidence_on_the_same_spread() -> None:
    ctx = CrossExchangeContext("binance", 60300.0, "okx", 60000.0)
    cheap = CrossExchangeArbStrategy(round_trip_cost_bps=2.0).generate_signal(ctx)
    dear = CrossExchangeArbStrategy(round_trip_cost_bps=20.0).generate_signal(ctx)
    assert cheap.confidence > dear.confidence


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(CrossExchangeArbStrategy())
    assert "cross_exchange_arb_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        CrossExchangeArbStrategy(max_capital_fraction=0.0)
