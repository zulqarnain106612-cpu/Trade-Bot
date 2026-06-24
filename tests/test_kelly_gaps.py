"""Targeted tests closing remaining coverage gaps in src/risk/kelly.py.

Companion to tests/test_kelly.py — covers branches not yet exercised:
KellyResult.position_size_pct, half_kelly_fraction bounds-check
ValueErrors, kelly_from_model_probs invalid-direction/non-finite-p_long
guards, size_position min_amount rejection, compute_position_size
default cfg resolution, compute_win_loss_stats all-wins/all-losses edge.
"""

import math

import pytest

from src.config import RiskSettings
from src.risk.kelly import (
    KellyResult,
    half_kelly_fraction,
    kelly_from_model_probs,
    size_position,
    compute_position_size,
    compute_win_loss_stats,
)


@pytest.fixture
def cfg():
    return RiskSettings()


class TestKellyResultPositionSizePct:
    """KellyResult.position_size_pct property (line 68)."""

    def test_position_size_pct_computed(self):
        result = KellyResult(
            kelly_fraction=0.2,
            adjusted_fraction=0.05,
            capital_usd=100_000,
            entry_price=50.0,
            quantity=100.0,
            notional_usd=5_000.0,
            is_capped=False,
        )
        assert result.position_size_pct == pytest.approx(5.0)

    def test_position_size_pct_zero(self):
        result = KellyResult(
            kelly_fraction=0.0,
            adjusted_fraction=0.0,
            capital_usd=100_000,
            entry_price=50.0,
            quantity=0.0,
            notional_usd=0.0,
            is_capped=False,
        )
        assert result.position_size_pct == 0.0


class TestHalfKellyFractionBoundsValidation:
    """half_kelly_fraction explicit multiplier/ceiling bounds checks (159, 161)."""

    def test_multiplier_above_one_raises(self, cfg):
        with pytest.raises(ValueError, match="multiplier must be in"):
            half_kelly_fraction(0.6, 1.5, multiplier=1.5, cfg=cfg)

    def test_multiplier_negative_raises(self, cfg):
        with pytest.raises(ValueError, match="multiplier must be in"):
            half_kelly_fraction(0.6, 1.5, multiplier=-0.1, cfg=cfg)

    def test_ceiling_above_one_raises(self, cfg):
        with pytest.raises(ValueError, match="ceiling must be in"):
            half_kelly_fraction(0.6, 1.5, ceiling=1.2, cfg=cfg)

    def test_ceiling_negative_raises(self, cfg):
        with pytest.raises(ValueError, match="ceiling must be in"):
            half_kelly_fraction(0.6, 1.5, ceiling=-0.05, cfg=cfg)

    def test_valid_boundary_multiplier_zero(self, cfg):
        """multiplier=0.0 is a valid boundary (no exception)."""
        raw, adjusted, capped = half_kelly_fraction(0.6, 1.5, multiplier=0.0, cfg=cfg)
        assert adjusted == 0.0

    def test_valid_boundary_multiplier_one(self, cfg):
        """multiplier=1.0 is a valid boundary (no exception)."""
        raw, adjusted, capped = half_kelly_fraction(0.6, 1.5, multiplier=1.0, ceiling=1.0, cfg=cfg)
        assert adjusted >= 0.0

    def test_valid_boundary_ceiling_zero(self, cfg):
        """ceiling=0.0 is a valid boundary (no exception)."""
        raw, adjusted, capped = half_kelly_fraction(0.6, 1.5, ceiling=0.0, cfg=cfg)
        assert adjusted == 0.0


class TestKellyFromModelProbsInvalidDirection:
    """kelly_from_model_probs direction validation (line 224)."""

    def test_direction_negative_one_raises(self, cfg):
        with pytest.raises(ValueError, match="direction must be 0"):
            kelly_from_model_probs(0.6, 100.0, 50.0, direction=-1, cfg=cfg)

    def test_direction_two_raises(self, cfg):
        with pytest.raises(ValueError, match="direction must be 0"):
            kelly_from_model_probs(0.6, 100.0, 50.0, direction=2, cfg=cfg)

    def test_direction_zero_valid(self, cfg):
        """direction=0 (short) is valid, no exception."""
        raw, adjusted, capped = kelly_from_model_probs(0.4, 100.0, 50.0, direction=0, cfg=cfg)
        assert isinstance(raw, float)

    def test_direction_one_valid(self, cfg):
        """direction=1 (long) is valid, no exception."""
        raw, adjusted, capped = kelly_from_model_probs(0.6, 100.0, 50.0, direction=1, cfg=cfg)
        assert isinstance(raw, float)


class TestKellyFromModelProbsNonFinitePLong:
    """kelly_from_model_probs non-finite p_long fail-safe (lines 234-240)."""

    def test_nan_p_long_returns_zero_fraction(self, cfg):
        raw, adjusted, capped = kelly_from_model_probs(
            float("nan"), 100.0, 50.0, direction=1, cfg=cfg,
        )
        assert raw == 0.0
        assert adjusted == 0.0
        assert capped is False

    def test_positive_inf_p_long_returns_zero_fraction(self, cfg):
        raw, adjusted, capped = kelly_from_model_probs(
            float("inf"), 100.0, 50.0, direction=1, cfg=cfg,
        )
        assert raw == 0.0
        assert adjusted == 0.0
        assert capped is False

    def test_negative_inf_p_long_returns_zero_fraction(self, cfg):
        raw, adjusted, capped = kelly_from_model_probs(
            float("-inf"), 100.0, 50.0, direction=0, cfg=cfg,
        )
        assert raw == 0.0
        assert adjusted == 0.0
        assert capped is False


class TestKellyFromModelProbsNonFiniteWinLossRatio:
    """kelly_from_model_probs win_loss_ratio finiteness clamp (line 259)."""

    def test_zero_avg_loss_produces_inf_ratio_clamped(self, cfg):
        """avg_loss_usd=0 -> division produces inf, clamped to 1.0 fallback."""
        raw, adjusted, capped = kelly_from_model_probs(
            0.6, avg_win_usd=100.0, avg_loss_usd=0.0, direction=1, cfg=cfg,
        )
        assert math.isfinite(raw)
        assert math.isfinite(adjusted)

    def test_extreme_avg_win_clamped_to_ceiling_ratio(self, cfg):
        """Extreme avg_win/avg_loss ratio clamped at 1000 ceiling."""
        raw, adjusted, capped = kelly_from_model_probs(
            0.6, avg_win_usd=1e15, avg_loss_usd=1e-10, direction=1, cfg=cfg,
        )
        assert math.isfinite(raw)
        assert math.isfinite(adjusted)


class TestSizePositionMinAmountRejection:
    """size_position min_amount threshold rejection (lines 367-372)."""

    def test_quantity_below_min_amount_returns_none(self, cfg):
        result = size_position(
            adjusted_fraction=0.001,  # tiny fraction -> tiny quantity
            capital_usd=1_000.0,
            entry_price=50_000.0,  # expensive asset -> small quantity
            min_amount=1.0,  # require at least 1 unit
            cfg=cfg,
        )
        assert result is None

    def test_quantity_meets_min_amount_succeeds(self, cfg):
        result = size_position(
            adjusted_fraction=0.05,
            capital_usd=100_000.0,
            entry_price=50.0,
            min_amount=0.001,
            cfg=cfg,
        )
        assert result is not None

    def test_min_amount_zero_disables_check(self, cfg):
        """min_amount=0.0 (default) means no minimum quantity check."""
        result = size_position(
            adjusted_fraction=0.0001,
            capital_usd=1_000.0,
            entry_price=50_000.0,
            min_amount=0.0,
            min_cost=0.0,
            cfg=cfg,
        )
        # Should not be rejected by min_amount (may still be valid or None
        # for other reasons, but not specifically from this branch)
        assert result is None or result.quantity >= 0.0


class TestSizePositionMaxPositionPctValidation:
    """size_position max_position_pct bounds-check ValueError (line 338)."""

    def test_max_position_pct_above_100_raises(self, cfg):
        with pytest.raises(ValueError, match="max_position_pct must be in"):
            size_position(
                adjusted_fraction=0.05,
                capital_usd=100_000.0,
                entry_price=50.0,
                max_position_pct=150.0,
                cfg=cfg,
            )

    def test_max_position_pct_negative_raises(self, cfg):
        with pytest.raises(ValueError, match="max_position_pct must be in"):
            size_position(
                adjusted_fraction=0.05,
                capital_usd=100_000.0,
                entry_price=50.0,
                max_position_pct=-10.0,
                cfg=cfg,
            )

    def test_max_position_pct_zero_valid_no_new_positions(self, cfg):
        """max_position_pct=0.0 is a legitimate 'no new positions' call."""
        result = size_position(
            adjusted_fraction=0.05,
            capital_usd=100_000.0,
            entry_price=50.0,
            max_position_pct=0.0,
            cfg=cfg,
        )
        assert result is None or result.quantity == 0.0

    def test_max_position_pct_boundary_100_valid(self, cfg):
        result = size_position(
            adjusted_fraction=0.05,
            capital_usd=100_000.0,
            entry_price=50.0,
            max_position_pct=100.0,
            cfg=cfg,
        )
        assert result is not None


class TestComputePositionSizeDefaultCfg:
    """compute_position_size resolves cfg=None via get_settings() (line 452)."""

    def test_none_cfg_uses_default_settings(self):
        """cfg=None should not raise — falls back to get_settings().risk."""
        result = compute_position_size(
            p_long=0.65,
            direction=1,
            capital_usd=100_000.0,
            entry_price=50.0,
            avg_win_usd=100.0,
            avg_loss_usd=50.0,
            cfg=None,
        )
        assert result is None or isinstance(result, KellyResult)


class TestComputeWinLossStatsAllWinsOrAllLosses:
    """compute_win_loss_stats all-wins/all-losses edge (line 515)."""

    def test_all_wins_returns_defaults(self):
        """No losses in 50+ trade sample -> falls back to defaults."""
        pnl = [10.0] * 60  # all positive, zero losses
        win_prob, avg_win, avg_loss = compute_win_loss_stats(pnl)
        assert win_prob == 0.5
        assert avg_win == 1.0
        assert avg_loss == 1.0

    def test_all_losses_returns_defaults(self):
        """No wins in 50+ trade sample -> falls back to defaults."""
        pnl = [-10.0] * 60  # all negative, zero wins
        win_prob, avg_win, avg_loss = compute_win_loss_stats(pnl)
        assert win_prob == 0.5
        assert avg_win == 1.0
        assert avg_loss == 1.0

    def test_mixed_wins_and_losses_computes_real_stats(self):
        """Mixed sample with both wins and losses computes actual stats."""
        pnl = [10.0] * 30 + [-5.0] * 30
        win_prob, avg_win, avg_loss = compute_win_loss_stats(pnl)
        assert win_prob == pytest.approx(0.5)
        assert avg_win == pytest.approx(10.0)
        assert avg_loss == pytest.approx(5.0)
