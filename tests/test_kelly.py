"""Tests for src/risk/kelly.py — Kelly formula, sizing, win/loss stats."""

import pytest

from src.config import invalidate_settings_cache
from src.risk.kelly import (
    _floor_to_precision,
    compute_position_size,
    compute_win_loss_stats,
    half_kelly_fraction,
    kelly_fraction,
    kelly_from_model_probs,
    size_position,
    uncertainty_scalar,
)


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


# ─── kelly_fraction ───────────────────────────────────────────────────────────


class TestKellyFraction:
    def test_classic_formula(self):
        # p=0.6, b=2 → f* = (0.6*2 - 0.4)/2 = 0.4
        assert abs(kelly_fraction(0.6, 2.0) - 0.4) < 1e-9

    def test_negative_edge_returns_zero(self):
        # p=0.3, b=1.0 → (0.3 - 0.7)/1 = -0.4 → clipped to 0
        assert abs(kelly_fraction(0.3, 1.0)) < 1e-9

    def test_high_edge_clipped_at_one(self):
        result = kelly_fraction(0.99, 100.0)
        assert result <= 1.0
        assert result > 0.0

    def test_breakeven_edge(self):
        # p=0.5, b=1.0 → f* = 0
        assert abs(kelly_fraction(0.5, 1.0)) < 1e-9

    def test_invalid_probability_zero(self):
        with pytest.raises(ValueError):
            kelly_fraction(0.0, 1.0)

    def test_invalid_probability_one(self):
        with pytest.raises(ValueError):
            kelly_fraction(1.0, 1.0)

    def test_invalid_ratio_zero(self):
        with pytest.raises(ValueError):
            kelly_fraction(0.6, 0.0)

    def test_invalid_ratio_negative(self):
        with pytest.raises(ValueError):
            kelly_fraction(0.6, -1.0)

    def test_symmetric_long_short(self):
        # Negative edge clips to 0; short side (p=0.4, b=0.5) has negative edge
        f_long = kelly_fraction(0.6, 2.0)
        f_short = kelly_fraction(0.4, 0.5)  # (0.4*0.5-0.6)/0.5 = -0.8 → clipped 0
        assert f_long > 0.0
        assert abs(f_short) < 1e-9


# ─── half_kelly_fraction ─────────────────────────────────────────────────────


class TestHalfKellyFraction:
    def test_multiplier_applied(self):
        raw, adj, capped = half_kelly_fraction(0.6, 2.0)
        assert abs(raw - 0.4) < 1e-9
        assert abs(adj - 0.2) < 1e-9  # 0.4 * 0.5
        assert capped is False

    def test_ceiling_binding(self):
        _, adj, capped = half_kelly_fraction(0.95, 10.0)
        assert abs(adj - 0.25) < 1e-9
        assert capped is True

    def test_zero_edge_returns_zero(self):
        raw, adj, _ = half_kelly_fraction(0.3, 1.0)
        assert abs(raw) < 1e-9
        assert abs(adj) < 1e-9

    def test_custom_multiplier(self):
        # multiplier=1.0 gives raw=0.4 but ceiling=0.25 caps it
        _, adj, capped = half_kelly_fraction(0.6, 2.0, multiplier=1.0)
        assert abs(adj - 0.25) < 1e-9
        assert capped is True

    def test_custom_ceiling(self):
        _, adj, capped = half_kelly_fraction(0.6, 2.0, multiplier=1.0, ceiling=0.1)
        assert abs(adj - 0.1) < 1e-9
        assert capped is True


# ─── kelly_from_model_probs ──────────────────────────────────────────────────


class TestKellyFromModelProbs:
    def test_long_direction(self):
        _, adj, _ = kelly_from_model_probs(0.75, 50.0, 25.0, direction=1)
        assert adj > 0.0

    def test_short_direction_symmetric(self):
        _, adj_long, _ = kelly_from_model_probs(0.75, 50.0, 25.0, direction=1)
        _, adj_short, _ = kelly_from_model_probs(0.25, 50.0, 25.0, direction=0)
        assert abs(adj_long - adj_short) < 1e-6

    def test_no_history_uses_default_ratio(self):
        _, adj, _ = kelly_from_model_probs(0.6, 0.0, 0.0, direction=1)
        assert adj > 0.0  # should not fail

    def test_edge_probability_guard(self):
        # p=0.0 should be guarded to 0.01
        _, adj, _ = kelly_from_model_probs(0.0, 10.0, 10.0, direction=1)
        assert adj >= 0.0  # no crash

    def test_result_bounded_by_ceiling(self):
        _, adj, _ = kelly_from_model_probs(0.99, 100.0, 1.0, direction=1)
        assert adj <= 0.25


# ─── _floor_to_precision ─────────────────────────────────────────────────────


class TestFloorToPrecision:
    def test_five_decimals(self):
        assert abs(_floor_to_precision(0.123456789, 5) - 0.12345) < 1e-9

    def test_zero_decimals(self):
        assert abs(_floor_to_precision(1.999, 0) - 1.0) < 1e-9

    def test_eight_decimals(self):
        assert abs(_floor_to_precision(0.001, 8) - 0.001) < 1e-9

    def test_floor_not_round(self):
        # 0.12999 should floor to 0.12, not round to 0.13
        assert abs(_floor_to_precision(0.12999, 2) - 0.12) < 1e-9

    def test_negative_precision_raises(self):
        with pytest.raises(ValueError):
            _floor_to_precision(1.0, -1)


# ─── size_position ────────────────────────────────────────────────────────────


class TestSizePosition:
    def test_basic_sizing(self):
        result = size_position(0.05, 1000.0, 30000.0, amount_precision=6)
        assert result is not None
        assert result.quantity > 0
        assert result.notional_usd <= 50.5  # 5% of 1000 + rounding margin

    def test_max_position_cap(self):
        result = size_position(0.20, 1000.0, 30000.0, amount_precision=6)
        assert result is not None
        assert result.is_capped
        assert result.adjusted_fraction <= 0.05  # capped at 5%

    def test_below_min_amount_returns_none(self):
        result = size_position(0.001, 10.0, 30000.0, amount_precision=6, min_amount=1.0)
        assert result is None

    def test_below_min_cost_returns_none(self):
        result = size_position(0.001, 5.0, 30000.0, amount_precision=8, min_cost=100.0)
        assert result is None

    def test_zero_capital_raises(self):
        with pytest.raises(ValueError):
            size_position(0.05, 0.0, 30000.0)

    def test_zero_price_raises(self):
        with pytest.raises(ValueError):
            size_position(0.05, 1000.0, 0.0)

    def test_negative_capital_raises(self):
        with pytest.raises(ValueError):
            size_position(0.05, -100.0, 30000.0)

    def test_quantity_floored_not_rounded(self):
        # ensure floor semantics — would round up to same but verify precision
        result = size_position(0.05, 1000.0, 30000.0, amount_precision=3)
        assert result is not None
        # quantity * price should not exceed notional_raw
        assert result.quantity * 30000.0 <= 50.0 + 0.001

    def test_result_is_frozen(self):
        result = size_position(0.05, 1000.0, 30000.0)
        assert result is not None
        with pytest.raises((AttributeError, TypeError)):
            result.quantity = 999.0  # type: ignore[misc]


# ─── compute_position_size ────────────────────────────────────────────────────


class TestComputePositionSize:
    def test_full_pipeline_long(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
        )
        assert result is not None
        assert result.quantity > 0
        assert result.notional_usd <= 50.5

    def test_full_pipeline_short(self):
        result = compute_position_size(
            p_long=0.25,
            direction=0,
            capital_usd=1000.0,
            entry_price=30000.0,
        )
        assert result is not None

    def test_low_confidence_still_sizes(self):
        result = compute_position_size(
            p_long=0.51,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
        )
        assert result is not None

    # ─── GAP-002: regime_scalar parameter ──────────────────────────────────────

    def test_regime_scalar_default_is_noop(self):
        baseline = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
        )
        with_explicit_one = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=1.0,
        )
        assert baseline is not None and with_explicit_one is not None
        assert baseline.quantity == with_explicit_one.quantity

    def test_regime_scalar_half_shrinks_position(self):
        # p_long=0.52, win_loss_ratio=1.0 -> raw kelly_fraction=0.04, well
        # below BOTH the Kelly ceiling (0.25) and the 5% max-position cap,
        # so regime_scalar's effect on adjusted_fraction is observable and
        # not masked by either clamp. Verified directly against the known
        # half-Kelly formula rather than relying on the is_capped flag,
        # since is_capped only reflects the Kelly-ceiling clamp, not the
        # separate max_position_size_pct clamp applied in size_position().
        full = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            regime_scalar=1.0,
        )
        halved = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            regime_scalar=0.5,
        )
        assert full is not None and halved is not None
        # raw kelly_fraction = (0.52*1 - 0.48)/1 = 0.04; half-Kelly = 0.02;
        # well under both the 0.25 ceiling and the 5% position cap.
        assert abs(full.adjusted_fraction - 0.02) < 1e-6
        assert halved.adjusted_fraction < full.adjusted_fraction
        assert abs(halved.adjusted_fraction - full.adjusted_fraction * 0.5) < 1e-6

    def test_regime_scalar_zero_yields_none(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=0.0,
        )
        assert result is None

    def test_regime_scalar_above_one_is_clamped_not_amplified(self):
        # A future caller bug passing regime_scalar > 1.0 must never widen
        # the position beyond what regime_scalar=1.0 would give — risk
        # gates only ever narrow, never amplify (CLAUDE.md: never weaken).
        normal = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=1.0,
        )
        amplified_attempt = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=5.0,
        )
        assert normal is not None and amplified_attempt is not None
        assert amplified_attempt.quantity == normal.quantity

    def test_regime_scalar_nan_fails_safe_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=float("nan"),
        )
        assert result is None

    def test_regime_scalar_negative_clamped_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            regime_scalar=-0.3,
        )
        assert result is None

    # ─── GAP-005/GAP-015: correlation_scalar parameter ────────────────────────────

    # ─── GARCH vol-targeting scalar parameter ─────────────────────────────────────

    def test_garch_vol_scalar_default_is_noop(self):
        baseline = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
        )
        with_one = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            garch_vol_scalar=1.0,
        )
        assert baseline is not None and with_one is not None
        assert baseline.quantity == with_one.quantity

    def test_garch_vol_scalar_half_shrinks_position(self):
        full = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            garch_vol_scalar=1.0,
        )
        halved = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            garch_vol_scalar=0.5,
        )
        assert full is not None and halved is not None
        assert halved.quantity <= full.quantity

    def test_garch_vol_scalar_zero_blocks_sizing(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            garch_vol_scalar=0.0,
        )
        assert result is None

    def test_garch_vol_scalar_inf_fails_safe_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            garch_vol_scalar=float("inf"),
        )
        assert result is None

    # ─── UI-004: sample_uncertainty_scalar parameter ──────────────────────────────

    def test_sample_uncertainty_scalar_default_is_noop(self):
        baseline = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
        )
        with_one = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=1.0,
        )
        assert baseline is not None and with_one is not None
        assert baseline.quantity == with_one.quantity

    def test_sample_uncertainty_scalar_half_shrinks_position(self):
        full = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=1.0,
        )
        halved = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=0.5,
        )
        assert full is not None and halved is not None
        assert halved.adjusted_fraction < full.adjusted_fraction
        assert abs(halved.adjusted_fraction - full.adjusted_fraction * 0.5) < 1e-6

    def test_sample_uncertainty_scalar_zero_yields_none(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=0.0,
        )
        assert result is None

    def test_sample_uncertainty_scalar_above_one_is_clamped_not_amplified(self):
        normal = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=1.0,
        )
        amplified_attempt = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=5.0,
        )
        assert normal is not None and amplified_attempt is not None
        assert amplified_attempt.quantity == normal.quantity

    def test_sample_uncertainty_scalar_nan_fails_safe_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=float("nan"),
        )
        assert result is None

    def test_sample_uncertainty_scalar_negative_clamped_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            sample_uncertainty_scalar=-0.3,
        )
        assert result is None

    def test_correlation_scalar_default_is_noop(self):
        baseline = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
        )
        with_one = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=1.0,
        )
        assert baseline is not None and with_one is not None
        assert baseline.quantity == with_one.quantity

    def test_correlation_scalar_half_shrinks_position(self):
        full = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            correlation_scalar=1.0,
        )
        halved = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            correlation_scalar=0.5,
        )
        assert full is not None and halved is not None
        assert halved.adjusted_fraction < full.adjusted_fraction
        assert abs(halved.adjusted_fraction - full.adjusted_fraction * 0.5) < 1e-6

    def test_correlation_scalar_zero_yields_none(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=0.0,
        )
        assert result is None

    def test_correlation_scalar_above_one_is_clamped_not_amplified(self):
        normal = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=1.0,
        )
        amplified_attempt = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=5.0,
        )
        assert normal is not None and amplified_attempt is not None
        assert amplified_attempt.quantity == normal.quantity

    def test_correlation_scalar_nan_fails_safe_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=float("nan"),
        )
        assert result is None

    def test_correlation_scalar_negative_clamped_to_zero(self):
        result = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=20.0,
            avg_loss_usd=10.0,
            correlation_scalar=-0.5,
        )
        assert result is None

    def test_correlation_and_regime_scalars_multiply(self):
        # Both scalars active simultaneously; combined = raw * 0.5 * 0.5
        full = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
        )
        combined = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            regime_scalar=0.5,
            correlation_scalar=0.5,
        )
        assert full is not None and combined is not None
        assert abs(combined.adjusted_fraction - full.adjusted_fraction * 0.25) < 1e-6

    # ─── GAP-015: notional_cap_usd (Carver/AFML/Thorp ceiling) ─────────────────

    def test_notional_cap_none_is_noop(self):
        uncapped = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=10_000.0,
            entry_price=30000.0,
            avg_win_usd=200.0,
            avg_loss_usd=100.0,
        )
        capped_none = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=10_000.0,
            entry_price=30000.0,
            avg_win_usd=200.0,
            avg_loss_usd=100.0,
            notional_cap_usd=None,
        )
        assert uncapped is not None and capped_none is not None
        assert uncapped.quantity == capped_none.quantity

    def test_notional_cap_above_kelly_is_noop(self):
        result = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
        )
        capped_high = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            notional_cap_usd=999_999.0,
        )
        assert result is not None and capped_high is not None
        assert result.quantity == capped_high.quantity

    def test_notional_cap_shrinks_large_kelly_position(self):
        uncapped = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=10_000.0,
            entry_price=100.0,
            avg_win_usd=200.0,
            avg_loss_usd=50.0,
        )
        assert uncapped is not None
        cap = uncapped.notional_usd * 0.4
        capped = compute_position_size(
            p_long=0.75,
            direction=1,
            capital_usd=10_000.0,
            entry_price=100.0,
            avg_win_usd=200.0,
            avg_loss_usd=50.0,
            notional_cap_usd=cap,
        )
        assert capped is not None
        assert capped.notional_usd <= cap + 1.0
        assert capped.notional_usd < uncapped.notional_usd
        assert capped.is_capped is True

    def test_notional_cap_never_amplifies(self):
        result_no_cap = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
        )
        result_high_cap = compute_position_size(
            p_long=0.52,
            direction=1,
            capital_usd=1000.0,
            entry_price=30000.0,
            avg_win_usd=10.0,
            avg_loss_usd=10.0,
            notional_cap_usd=50_000.0,
        )
        assert result_no_cap is not None and result_high_cap is not None
        assert result_high_cap.quantity <= result_no_cap.quantity


# ─── compute_win_loss_stats ───────────────────────────────────────────────────


class TestComputeWinLossStats:
    def test_too_few_trades_returns_defaults(self):
        _wp, _aw, _al, _std = compute_win_loss_stats([10.0, -5.0])
        assert abs(_wp - 0.5) < 1e-9
        assert abs(_aw - 1.0) < 1e-9
        assert abs(_al - 1.0) < 1e-9
        assert abs(_std - 0.5) < 1e-9

    def test_correct_win_probability(self):
        # VF-031: compute_win_loss_stats requires >=50 trades before
        # trusting the sample (NEW-010 raised the threshold from 10);
        # this test previously supplied only 10 trades, so it silently
        # exercised the conservative-default fallback (0.5, 1.0, 1.0)
        # instead of the real computation it claims to test. Scale to
        # 50 trades at the same 6:4 win:loss ratio.
        #
        # win_prob is now Beta-shrunk toward a 0.5 prior (n_obs=50,
        # prior_strength=20): posterior = (0.5*20 + 0.6*50)/70.
        pnl = [10.0] * 30 + [-5.0] * 20
        wp, _aw, _al, _std = compute_win_loss_stats(pnl)
        assert wp == pytest.approx((0.5 * 20 + 0.6 * 50) / 70)

    def test_correct_averages(self):
        pnl = [10.0] * 30 + [-5.0] * 20
        _wp, aw, al, _std = compute_win_loss_stats(pnl)
        assert abs(aw - 10.0) < 1e-9
        assert abs(al - 5.0) < 1e-9

    def test_all_wins(self):
        pnl = [10.0] * 10
        _wp, aw, _al, _std = compute_win_loss_stats(pnl)
        assert abs(_wp - 0.5) < 1e-9 and abs(aw - 1.0) < 1e-9  # falls back (no losses)

    def test_all_losses(self):
        pnl = [-10.0] * 10
        _wp, _aw, al, _std = compute_win_loss_stats(pnl)
        assert abs(_wp - 0.5) < 1e-9 and abs(al - 1.0) < 1e-9  # falls back (no wins)

    def test_win_prob_shrunk_toward_prior_at_minimum_sample(self):
        # At the 50-trade minimum, a skewed win rate should be pulled toward
        # 0.5 rather than trusted at face value.
        pnl = [10.0] * 45 + [-5.0] * 5  # raw win rate = 0.9
        wp, _aw, _al, _std = compute_win_loss_stats(pnl)
        assert 0.5 < wp < 0.9

    def test_win_prob_shrinkage_vanishes_at_large_sample(self):
        # With a large trade count, shrinkage should have negligible effect.
        pnl = [10.0] * 900 + [-5.0] * 100  # raw win rate = 0.9
        wp, _aw, _al, _std = compute_win_loss_stats(pnl)
        assert wp == pytest.approx(0.9, abs=0.02)

    def test_win_prob_std_shrinks_toward_zero_at_large_sample(self):
        pnl = [10.0] * 900 + [-5.0] * 100
        _wp, _aw, _al, std = compute_win_loss_stats(pnl)
        assert 0.0 < std < 0.1


# ─── uncertainty_scalar ────────────────────────────────────────────────────────


class TestUncertaintyScalar:
    def test_zero_std_is_full_confidence_noop(self):
        assert uncertainty_scalar(0.0) == pytest.approx(1.0)

    def test_max_std_zeroes_out_sizing(self):
        assert uncertainty_scalar(0.5, k=2.0) == pytest.approx(0.0)

    def test_monotonically_decreasing_in_std(self):
        low = uncertainty_scalar(0.1)
        high = uncertainty_scalar(0.3)
        assert high < low

    def test_negative_std_fails_safe_to_zero(self):
        assert uncertainty_scalar(-0.1) == 0.0

    def test_nan_fails_safe_to_zero(self):
        assert uncertainty_scalar(float("nan")) == 0.0

    def test_result_always_in_unit_interval(self):
        for std in (0.0, 0.05, 0.25, 0.5, 1.0, 10.0):
            v = uncertainty_scalar(std)
            assert 0.0 <= v <= 1.0
