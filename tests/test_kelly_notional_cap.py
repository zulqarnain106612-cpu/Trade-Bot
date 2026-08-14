"""
The GAP-015 Carver/AFML/Thorp notional cap in compute_position_size().

A ceiling has exactly one forbidden outcome: letting through a position
larger than the ceiling allows. These tests pin the three paths where the
previous implementation did precisely that.
"""

from __future__ import annotations

import math

import pytest

from src.config import RiskSettings
from src.risk.kelly import compute_position_size


@pytest.fixture
def cfg():
    return RiskSettings()


_BASE = {
    "p_long": 0.9,
    "direction": 1,
    "capital_usd": 100_000.0,
    "entry_price": 50.0,
    "avg_win_usd": 100.0,
    "avg_loss_usd": 50.0,
}


def _uncapped(cfg, **overrides):
    result = compute_position_size(**{**_BASE, **overrides}, cfg=cfg)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# The cap must never fail open
# ---------------------------------------------------------------------------


class TestCapNeverFailsOpen:
    def test_cap_tighter_than_one_quantisation_step_skips_the_trade(self, cfg):
        """
        A cap below one quantisation step used to leave the *uncapped*
        result in place and return it — the full Kelly position, taken
        because the ceiling was too small to express.
        """
        uncapped = _uncapped(cfg, amount_precision=0.0)
        # precision=0 means whole units; a $10 cap at $50/unit is 0.2 units,
        # which floors to 0.
        result = compute_position_size(
            **_BASE, amount_precision=0.0, notional_cap_usd=10.0, cfg=cfg
        )
        assert result is None, f"cap of $10 must not admit a ${uncapped.notional_usd:.2f} position"

    def test_zero_cap_is_a_veto_not_an_absent_cap(self, cfg):
        """
        recommend_position_notional() returns exactly 0.0 to mean "every
        sizing method agrees there is no edge" (UI-007). Treating that as
        "no cap" discarded a unanimous no-trade veto on the live path.
        """
        assert compute_position_size(**_BASE, notional_cap_usd=0.0, cfg=cfg) is None

    def test_negative_cap_admits_no_position(self, cfg):
        assert compute_position_size(**_BASE, notional_cap_usd=-100.0, cfg=cfg) is None

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_cap_fails_closed(self, cfg, bad):
        # `cap > 0.0` is False for NaN, so an unchecked NaN reads as "no cap"
        # and silently removes a ceiling the caller asked for.
        assert compute_position_size(**_BASE, notional_cap_usd=bad, cfg=cfg) is None


# ---------------------------------------------------------------------------
# The capped quantity must clear the same exchange filters as any other
# ---------------------------------------------------------------------------


class TestCappedSizeRespectsExchangeMinimums:
    def test_capped_below_min_amount_skips_the_trade(self, cfg):
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        capped_qty = cap / _BASE["entry_price"]

        # min_amount set just above what the cap can buy: the cap and the
        # exchange minimum are irreconcilable, so there is no valid position.
        result = compute_position_size(
            **_BASE, notional_cap_usd=cap, min_amount=capped_qty * 1.5, cfg=cfg
        )
        assert result is None

    def test_capped_below_min_cost_skips_the_trade(self, cfg):
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        result = compute_position_size(**_BASE, notional_cap_usd=cap, min_cost=cap * 1.5, cfg=cfg)
        assert result is None

    def test_capped_size_that_clears_the_minimums_is_returned(self, cfg):
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        result = compute_position_size(
            **_BASE,
            notional_cap_usd=cap,
            min_amount=0.001,
            min_cost=1.0,
            cfg=cfg,
        )
        assert result is not None
        assert result.notional_usd <= cap
        assert result.is_capped is True


# ---------------------------------------------------------------------------
# The recorded fraction must describe the position actually taken
# ---------------------------------------------------------------------------


class TestCappedResultIsInternallyConsistent:
    def test_adjusted_fraction_reflects_the_capped_notional(self, cfg):
        """
        Both executors write adjusted_fraction into the trade record, so a
        stale pre-cap value books a Kelly fraction the position never had.
        """
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        result = compute_position_size(**_BASE, notional_cap_usd=cap, cfg=cfg)

        assert result is not None
        assert result.adjusted_fraction < uncapped.adjusted_fraction
        assert result.adjusted_fraction == pytest.approx(
            result.notional_usd / result.capital_usd, rel=1e-3
        )

    def test_kelly_fraction_is_preserved_as_the_pre_cap_record(self, cfg):
        # kelly_fraction documents what Kelly asked for; only the adjusted
        # fraction describes what was taken.
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        result = compute_position_size(**_BASE, notional_cap_usd=cap, cfg=cfg)

        assert result is not None
        assert result.kelly_fraction == pytest.approx(uncapped.kelly_fraction)

    def test_notional_matches_quantity_times_price(self, cfg):
        uncapped = _uncapped(cfg)
        cap = uncapped.notional_usd * 0.5
        result = compute_position_size(**_BASE, notional_cap_usd=cap, cfg=cfg)

        assert result is not None
        assert result.notional_usd == pytest.approx(
            round(result.quantity * _BASE["entry_price"], 2)
        )


# ---------------------------------------------------------------------------
# Unchanged behaviour
# ---------------------------------------------------------------------------


class TestUncappedPathsUnchanged:
    def test_none_still_means_no_cap(self, cfg):
        assert compute_position_size(**_BASE, notional_cap_usd=None, cfg=cfg) is not None

    def test_cap_above_the_position_is_a_no_op(self, cfg):
        uncapped = _uncapped(cfg)
        result = compute_position_size(
            **_BASE, notional_cap_usd=uncapped.notional_usd * 10.0, cfg=cfg
        )
        assert result is not None
        assert result.quantity == pytest.approx(uncapped.quantity)
        assert result.is_capped == uncapped.is_capped

    def test_cap_exactly_equal_to_the_position_does_not_requantise(self, cfg):
        # The comparison is strict '>', so the boundary must pass through
        # untouched rather than round-tripping through the cap path.
        uncapped = _uncapped(cfg)
        result = compute_position_size(**_BASE, notional_cap_usd=uncapped.notional_usd, cfg=cfg)
        assert result is not None
        assert result.quantity == pytest.approx(uncapped.quantity)
        assert result.notional_usd == pytest.approx(uncapped.notional_usd)
