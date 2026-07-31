"""Tests for the v10 capital preservation floor."""

from __future__ import annotations

import math

import pytest

from src.risk.capital_preservation_floor import CapitalPreservationFloor


def test_rejects_invalid_max_drawdown_pct() -> None:
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        CapitalPreservationFloor(max_drawdown_pct=0.0)
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        CapitalPreservationFloor(max_drawdown_pct=1.0)


def test_no_halt_within_drawdown_limit() -> None:
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    assert floor.update_equity(10000.0)
    assert floor.update_equity(8000.0)  # 20% drawdown, under 30% floor
    assert not floor.is_halted


def test_halts_when_drawdown_exceeds_floor() -> None:
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    proceed = floor.update_equity(6900.0)  # 31% drawdown
    assert not proceed
    assert floor.is_halted
    assert "drawdown" in floor.halt_reason


def test_halted_state_persists_through_equity_recovery() -> None:
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    floor.update_equity(6000.0)  # halts
    assert floor.is_halted
    proceed = floor.update_equity(9999.0)  # recovers almost fully
    assert not proceed
    assert floor.is_halted


def test_re_authorize_clears_halt() -> None:
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    floor.update_equity(6000.0)
    assert floor.is_halted

    floor.re_authorize(authorized_by="ops_lead", reason="reviewed and approved", at_ms=123)
    assert not floor.is_halted
    assert floor.halt_reason == ""
    assert floor.last_reauthorization is not None
    assert floor.last_reauthorization.authorized_by == "ops_lead"


def test_re_authorize_rejects_empty_authorized_by() -> None:
    floor = CapitalPreservationFloor()
    with pytest.raises(ValueError, match="authorized_by"):
        floor.re_authorize(authorized_by="", reason="x", at_ms=0)


def test_rejects_negative_equity() -> None:
    floor = CapitalPreservationFloor()
    with pytest.raises(ValueError, match="equity_usd"):
        floor.update_equity(-1.0)


def test_no_reauthorization_initially_none() -> None:
    floor = CapitalPreservationFloor()
    assert floor.last_reauthorization is None


def test_floor_remains_sensitive_to_prior_peak_after_reauth() -> None:
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    floor.update_equity(6000.0)  # halts at peak=10000
    floor.re_authorize(authorized_by="ops", reason="ok", at_ms=1)
    # Still measured against the same peak (10000) until a new peak forms.
    proceed = floor.update_equity(6500.0)  # still 35% down from peak=10000
    assert not proceed
    assert floor.is_halted


# ---------------------------------------------------------------------------
# Non-finite marks — a corrupt mark must not reach _peak_equity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_rejects_non_finite_equity(bad: float) -> None:
    floor = CapitalPreservationFloor()
    with pytest.raises(ValueError, match="finite"):
        floor.update_equity(bad)


def test_nan_mark_does_not_slip_past_the_floor() -> None:
    """`nan < 0.0` is False, so the negative guard alone does not catch it."""
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    with pytest.raises(ValueError, match="finite"):
        floor.update_equity(math.nan)
    # The floor is still armed and still measured against the real peak.
    assert not floor.update_equity(6000.0)
    assert floor.is_halted


def test_inf_mark_does_not_poison_the_peak() -> None:
    """
    max(peak, inf) would set _peak_equity = inf permanently, after which
    every drawdown is (inf - equity) / inf = nan and never trips the floor.
    One bad mark would disable the outermost backstop for the life of the
    process, so the mark is rejected before it is stored.
    """
    floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
    floor.update_equity(10000.0)
    with pytest.raises(ValueError, match="finite"):
        floor.update_equity(math.inf)

    # Peak must still be 10000, not inf: a 40% drawdown still halts.
    assert not floor.update_equity(6000.0)
    assert floor.is_halted
    assert "peak=10000.00" in floor.halt_reason
