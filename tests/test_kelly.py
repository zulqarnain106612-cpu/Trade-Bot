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
        f_long  = kelly_fraction(0.6, 2.0)
        f_short = kelly_fraction(0.4, 0.5)   # (0.4*0.5-0.6)/0.5 = -0.8 → clipped 0
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
            p_long=0.75, direction=1,
            capital_usd=1000.0, entry_price=30000.0,
            avg_win_usd=20.0, avg_loss_usd=10.0,
        )
        assert result is not None
        assert result.quantity > 0
        assert result.notional_usd <= 50.5

    def test_full_pipeline_short(self):
        result = compute_position_size(
            p_long=0.25, direction=0,
            capital_usd=1000.0, entry_price=30000.0,
        )
        assert result is not None

    def test_low_confidence_still_sizes(self):
        result = compute_position_size(
            p_long=0.51, direction=1,
            capital_usd=1000.0, entry_price=30000.0,
        )
        assert result is not None


# ─── compute_win_loss_stats ───────────────────────────────────────────────────


class TestComputeWinLossStats:
    def test_too_few_trades_returns_defaults(self):
        _wp, _aw, _al = compute_win_loss_stats([10.0, -5.0])
        assert abs(_wp - 0.5) < 1e-9
        assert abs(_aw - 1.0) < 1e-9
        assert abs(_al - 1.0) < 1e-9

    def test_correct_win_probability(self):
        # VF-031: compute_win_loss_stats requires >=50 trades before
        # trusting the sample (NEW-010 raised the threshold from 10);
        # this test previously supplied only 10 trades, so it silently
        # exercised the conservative-default fallback (0.5, 1.0, 1.0)
        # instead of the real computation it claims to test. Scale to
        # 50 trades at the same 6:4 win:loss ratio.
        pnl = [10.0] * 30 + [-5.0] * 20
        wp, _aw, _al = compute_win_loss_stats(pnl)
        assert abs(wp - 0.6) < 1e-9

    def test_correct_averages(self):
        pnl = [10.0] * 30 + [-5.0] * 20
        wp, aw, al = compute_win_loss_stats(pnl)
        assert abs(aw - 10.0) < 1e-9
        assert abs(al - 5.0) < 1e-9

    def test_all_wins(self):
        pnl = [10.0] * 10
        _wp, aw, _al = compute_win_loss_stats(pnl)
        assert abs(_wp - 0.5) < 1e-9 and abs(aw - 1.0) < 1e-9  # falls back (no losses)

    def test_all_losses(self):
        pnl = [-10.0] * 10
        _wp, _aw, al = compute_win_loss_stats(pnl)
        assert abs(_wp - 0.5) < 1e-9 and abs(al - 1.0) < 1e-9  # falls back (no wins)
