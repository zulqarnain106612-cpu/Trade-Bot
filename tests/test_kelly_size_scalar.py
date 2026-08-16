"""
Tests for apply_size_scalar — the path a post-sizing ceiling takes to reach
an actual order.

Kelly runs before the risk gates, so every scalar those gates produce
arrives after the quantity has been computed and quantised. Rescaling the
fraction alone is not enough: the quantity has to be requantised and
rechecked against the exchange minimums, because a shrunk order is a
different order.
"""

from __future__ import annotations

import pytest

from src.risk.kelly import KellyResult, apply_size_scalar


def _result(notional: float = 1000.0, entry_price: float = 100.0) -> KellyResult:
    return KellyResult(
        kelly_fraction=0.2,
        adjusted_fraction=0.1,
        capital_usd=10_000.0,
        entry_price=entry_price,
        quantity=notional / entry_price,
        notional_usd=notional,
        is_capped=False,
    )


def test_scalar_of_one_is_a_no_op_and_does_not_requantise() -> None:
    # No reduction was asked for, so round-tripping through quantisation
    # could only lose precision.
    original = _result()
    assert apply_size_scalar(original, 1.0, original.entry_price) is original


@pytest.mark.parametrize("scalar", [0.0, -0.5, 1.5])
def test_scalar_outside_the_unit_interval_is_rejected(scalar: float) -> None:
    # Above 1.0 would grow the position; 0.0 is a veto, not a scalar.
    with pytest.raises(ValueError, match="scalar"):
        apply_size_scalar(_result(), scalar, 100.0)


def test_halving_halves_the_notional() -> None:
    reduced = apply_size_scalar(_result(1000.0), 0.5, 100.0)
    assert reduced is not None
    assert reduced.notional_usd == pytest.approx(500.0, abs=0.01)
    assert reduced.quantity == pytest.approx(5.0, abs=1e-6)


def test_reduction_recomputes_the_adjusted_fraction() -> None:
    # The fraction is written into the trade record by both executors;
    # leaving the pre-reduction value would record a size the position
    # never had.
    original = _result(1000.0)
    reduced = apply_size_scalar(original, 0.5, 100.0)
    assert reduced is not None
    assert reduced.adjusted_fraction < original.adjusted_fraction
    assert reduced.adjusted_fraction == pytest.approx(
        reduced.notional_usd / reduced.capital_usd, rel=1e-6
    )


def test_reduction_marks_the_result_as_capped() -> None:
    reduced = apply_size_scalar(_result(), 0.5, 100.0)
    assert reduced is not None
    assert reduced.is_capped is True


def test_quantity_is_requantised_to_exchange_precision() -> None:
    # 0-decimal precision: the shrunk quantity must floor to a whole unit
    # rather than carry fractional size the exchange would reject.
    reduced = apply_size_scalar(_result(1000.0), 0.55, 100.0, amount_precision=0.0)
    assert reduced is not None
    assert reduced.quantity == pytest.approx(5.0)


def test_reduction_below_min_amount_skips_the_trade() -> None:
    # None means skip. Taking the trade at its unreduced size because the
    # reduction was inconvenient is the one outcome a ceiling must never
    # produce.
    assert apply_size_scalar(_result(1000.0), 0.5, 100.0, min_amount=8.0) is None


def test_reduction_below_min_cost_skips_the_trade() -> None:
    assert apply_size_scalar(_result(1000.0), 0.5, 100.0, min_cost=800.0) is None


def test_reduction_below_one_precision_step_skips_the_trade() -> None:
    # The reduced quantity floors to zero: there is no position to take.
    tiny = _result(notional=1.0, entry_price=100.0)
    assert apply_size_scalar(tiny, 0.1, 100.0, amount_precision=0.0) is None


def test_non_positive_entry_price_skips_rather_than_dividing_by_zero() -> None:
    assert apply_size_scalar(_result(), 0.5, 0.0) is None


def test_scalars_compose_monotonically() -> None:
    base = _result(1000.0)
    half = apply_size_scalar(base, 0.5, 100.0)
    quarter = apply_size_scalar(base, 0.25, 100.0)
    assert half is not None and quarter is not None
    assert quarter.notional_usd < half.notional_usd < base.notional_usd


def test_a_zero_entry_price_on_the_result_must_not_be_used_for_sizing() -> None:
    # KellyResult.entry_price can legitimately be 0.0 — the orchestrator has a
    # block dedicated to resolving it from a ticker before submission. Sizing
    # against the unresolved value refuses the reduction and skips a valid
    # trade, reporting it as an agreement reduction: the wrong diagnosis for a
    # price that was already fixed. The caller must pass the resolved price.
    unpriced = KellyResult(
        kelly_fraction=0.2,
        adjusted_fraction=0.1,
        capital_usd=10_000.0,
        entry_price=0.0,
        quantity=10.0,
        notional_usd=1000.0,
        is_capped=False,
    )
    assert apply_size_scalar(unpriced, 0.4, unpriced.entry_price) is None

    resolved = apply_size_scalar(unpriced, 0.4, 100.0)
    assert resolved is not None
    assert resolved.notional_usd == pytest.approx(400.0, abs=0.01)
    assert resolved.quantity == pytest.approx(4.0, abs=1e-6)
