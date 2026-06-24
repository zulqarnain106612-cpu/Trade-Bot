"""Test coverage for src/strategies/position_sizing.py — Carver/AFML/Thorp sizing."""

import numpy as np
import pytest

from src.strategies.position_sizing import (
    carver_forecast_position,
    vol_target_quantity,
    estimate_daily_vol,
    correlation_adjusted_notional,
    afml_bet_size,
    thorp_kelly_with_variance,
    recommend_position_notional,
)


class TestCarverForecastPosition:
    """Carver (2019) Ch.4 forecast-scaled sizing."""

    def test_positive_forecast_gives_notional(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional > 0

    def test_zero_forecast_gives_zero(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=0.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_negative_forecast_uses_abs(self):
        """Direction-agnostic: negative forecast still produces positive notional."""
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=-10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional > 0

    def test_zero_vol_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=10.0, daily_vol_pct=0.0, price=100.0,
        )
        assert notional == 0.0

    def test_zero_price_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=10.0, daily_vol_pct=0.02, price=0.0,
        )
        assert notional == 0.0

    def test_zero_capital_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=0.0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_negative_capital_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=-5000.0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_forecast_clipped_at_max(self):
        """Forecast beyond ±20 is clipped."""
        notional_clipped = carver_forecast_position(
            capital_usd=10000.0, forecast=100.0, daily_vol_pct=0.02, price=100.0,
        )
        notional_at_cap = carver_forecast_position(
            capital_usd=10000.0, forecast=20.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional_clipped == notional_at_cap

    def test_capped_at_25_percent_of_capital(self):
        """Position never exceeds 25% of capital (Kelly ceiling)."""
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=20.0, daily_vol_pct=0.001, price=100.0,
        )
        assert notional <= 10000.0 * 0.25

    def test_custom_vol_target(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
            daily_vol_target_pct=0.5,
        )
        assert notional > 0

    def test_custom_forecast_scalar(self):
        notional = carver_forecast_position(
            capital_usd=10000.0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
            forecast_scalar=20.0,
        )
        assert notional >= 0


class TestVolTargetQuantity:
    """Carver (2019) Ch.2 volatility targeting."""

    def test_basic_quantity_calculation(self):
        qty = vol_target_quantity(
            capital_usd=10000.0, price=100.0, daily_vol_pct=0.02,
        )
        assert qty > 0

    def test_zero_vol_returns_zero(self):
        qty = vol_target_quantity(capital_usd=10000.0, price=100.0, daily_vol_pct=0.0)
        assert qty == 0.0

    def test_zero_price_returns_zero(self):
        qty = vol_target_quantity(capital_usd=10000.0, price=0.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_zero_capital_returns_zero(self):
        qty = vol_target_quantity(capital_usd=0.0, price=100.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_negative_capital_returns_zero(self):
        qty = vol_target_quantity(capital_usd=-1000.0, price=100.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_higher_vol_lower_quantity(self):
        """Higher volatility → smaller position size for same vol target."""
        qty_low_vol = vol_target_quantity(capital_usd=10000.0, price=100.0, daily_vol_pct=0.01)
        qty_high_vol = vol_target_quantity(capital_usd=10000.0, price=100.0, daily_vol_pct=0.05)
        assert qty_low_vol > qty_high_vol

    def test_custom_vol_target_pct(self):
        qty = vol_target_quantity(
            capital_usd=10000.0, price=100.0, daily_vol_pct=0.02,
            daily_vol_target_pct=1.0,
        )
        assert qty > 0


class TestEstimateDailyVol:
    """EWM std of log returns for daily vol estimation."""

    def test_trending_series_positive_vol(self):
        close = np.linspace(100, 110, 50)
        vol = estimate_daily_vol(close)
        assert vol > 0

    def test_flat_series_minimal_vol(self):
        close = [100.0] * 50
        vol = estimate_daily_vol(close)
        assert vol >= 1e-6

    def test_volatile_series_higher_vol(self):
        """More volatile series → higher estimated vol."""
        np.random.seed(1)
        stable = 100 + np.cumsum(np.random.randn(100) * 0.1)
        volatile = 100 + np.cumsum(np.random.randn(100) * 5.0)
        vol_stable = estimate_daily_vol(stable)
        vol_volatile = estimate_daily_vol(volatile)
        assert vol_volatile > vol_stable

    def test_short_series_fallback(self):
        """Series < 2 elements → fallback 1%."""
        vol = estimate_daily_vol([100.0])
        assert vol == 0.01

    def test_empty_series_fallback(self):
        vol = estimate_daily_vol([])
        assert vol == 0.01

    def test_two_element_series(self):
        """Exactly 2 elements → log_ret has 1 element → fallback."""
        vol = estimate_daily_vol([100.0, 101.0])
        assert vol == 0.01

    def test_custom_window(self):
        close = np.linspace(100, 120, 60)
        vol = estimate_daily_vol(close, window=10)
        assert vol > 0

    def test_accepts_list_input(self):
        close = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        vol = estimate_daily_vol(close)
        assert isinstance(vol, float)

    def test_accepts_ndarray_input(self):
        close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        vol = estimate_daily_vol(close)
        assert isinstance(vol, float)


class TestCorrelationAdjustedNotional:
    """AFML Ch.16 correlation-aware position reduction."""

    def test_below_threshold_no_reduction(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=0.5,
        )
        assert notional == 1000.0

    def test_at_threshold_no_reduction(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=0.7,
        )
        assert notional == 1000.0

    def test_above_threshold_reduces(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=0.85,
        )
        assert 0 < notional < 1000.0

    def test_full_correlation_zero_notional(self):
        """Correlation = 1.0 → reduction factor = 0."""
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=1.0,
        )
        assert notional == 0.0

    def test_custom_threshold(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=0.6,
            threshold=0.5,
        )
        assert notional < 1000.0

    def test_zero_correlation_full_notional(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=0.0,
        )
        assert notional == 1000.0

    def test_negative_correlation_full_notional(self):
        """Negative correlation (hedging) → no reduction, below threshold."""
        notional = correlation_adjusted_notional(
            proposed_notional_usd=1000.0, avg_correlation_with_book=-0.5,
        )
        assert notional == 1000.0


class TestAfmlBetSize:
    """AFML Ch.10 bet-sizing from model probability: f = 2p - 1."""

    def test_no_edge_at_p_half(self):
        """p=0.5 → no edge → zero bet."""
        size = afml_bet_size(p_long=0.5, capital_usd=10000.0)
        assert size == 0.0

    def test_moderate_edge(self):
        """p=0.7 → raw edge f=0.4, capped by default max_fraction=0.25."""
        size = afml_bet_size(p_long=0.7, capital_usd=10000.0)
        assert size == pytest.approx(10000.0 * 0.25, rel=1e-6)

    def test_moderate_edge_uncapped(self):
        """p=0.7 → f=0.4 when max_fraction is raised above the edge."""
        size = afml_bet_size(p_long=0.7, capital_usd=10000.0, max_fraction=0.5)
        assert size == pytest.approx(10000.0 * 0.4, rel=1e-6)

    def test_strong_edge_capped(self):
        """p=0.9 → f=0.8, capped at max_fraction=0.25."""
        size = afml_bet_size(p_long=0.9, capital_usd=10000.0, max_fraction=0.25)
        assert size == pytest.approx(10000.0 * 0.25, rel=1e-6)

    def test_below_half_negative_edge_zero(self):
        """p < 0.5 → negative edge → zero bet."""
        size = afml_bet_size(p_long=0.3, capital_usd=10000.0)
        assert size == 0.0

    def test_p_equals_one_max_edge(self):
        size = afml_bet_size(p_long=1.0, capital_usd=10000.0, max_fraction=0.5)
        assert size == pytest.approx(10000.0 * 0.5, rel=1e-6)

    def test_p_equals_zero_zero_bet(self):
        size = afml_bet_size(p_long=0.0, capital_usd=10000.0)
        assert size == 0.0

    def test_custom_max_fraction(self):
        size = afml_bet_size(p_long=0.8, capital_usd=10000.0, max_fraction=0.1)
        assert size == pytest.approx(10000.0 * 0.1, rel=1e-6)


class TestThorpKellyWithVariance:
    """Thorp (2006) fractional Kelly with variance penalty."""

    def test_positive_edge_gives_notional(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
        )
        assert notional > 0

    def test_invalid_win_prob_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.0, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
        )
        assert notional == 0.0

    def test_invalid_win_prob_one(self):
        notional = thorp_kelly_with_variance(
            win_prob=1.0, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
        )
        assert notional == 0.0

    def test_zero_win_loss_ratio_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=0.0, capital_usd=10000.0, price=100.0,
        )
        assert notional == 0.0

    def test_negative_kelly_clamped_to_zero(self):
        """Poor win_prob/ratio combo → negative Kelly clamped to 0."""
        notional = thorp_kelly_with_variance(
            win_prob=0.3, win_loss_ratio=0.5, capital_usd=10000.0, price=100.0,
        )
        assert notional == 0.0

    def test_zero_price_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=0.0,
        )
        assert notional == 0.0

    def test_variance_penalty_reduces_size(self):
        notional_no_penalty = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            variance_penalty=0.0,
        )
        notional_with_penalty = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            variance_penalty=0.4,
        )
        assert notional_with_penalty < notional_no_penalty

    def test_kelly_ceiling_caps_size(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.95, win_loss_ratio=5.0, capital_usd=10000.0, price=100.0,
            kelly_multiplier=1.0, kelly_ceiling=0.1,
        )
        assert notional <= 10000.0 * 0.1

    def test_custom_kelly_multiplier(self):
        full_kelly = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            kelly_multiplier=1.0, kelly_ceiling=1.0,
        )
        half_kelly = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            kelly_multiplier=0.5, kelly_ceiling=1.0,
        )
        assert half_kelly == pytest.approx(full_kelly * 0.5, rel=1e-6)

    def test_variance_penalty_clipped_at_half(self):
        """variance_penalty clipped to [0, 0.5] max reduction."""
        notional_extreme = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            variance_penalty=10.0,
        )
        notional_at_cap = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=10000.0, price=100.0,
            variance_penalty=0.5,
        )
        assert notional_extreme == pytest.approx(notional_at_cap, rel=1e-6)


class TestRecommendPositionNotional:
    """Combined sizing recommendation — most conservative of all methods."""

    def test_returns_all_method_keys(self):
        result = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=10.0,
            daily_vol_pct=0.02,
        )
        assert set(result.keys()) == {
            "thorp_kelly", "afml_bet_size", "carver_forecast",
            "correlation_adjusted", "recommended",
        }

    def test_recommended_is_minimum_of_methods(self):
        result = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=10.0,
            daily_vol_pct=0.02,
        )
        method_values = [
            result["thorp_kelly"], result["afml_bet_size"],
            result["carver_forecast"], result["correlation_adjusted"],
        ]
        assert result["recommended"] >= min(10.0, min(method_values))

    def test_recommended_never_below_min_notional(self):
        """Recommended floor is _MIN_NOTIONAL_USD (10.0)."""
        result = recommend_position_notional(
            capital_usd=100.0, price=100.0, p_long=0.5,
            win_prob=0.5, win_loss_ratio=1.0, forecast=0.0,
            daily_vol_pct=0.02,
        )
        assert result["recommended"] >= 10.0

    def test_high_correlation_reduces_recommendation(self):
        result_low_corr = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.8,
            win_prob=0.65, win_loss_ratio=1.8, forecast=15.0,
            daily_vol_pct=0.02, avg_book_correlation=0.0,
        )
        result_high_corr = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.8,
            win_prob=0.65, win_loss_ratio=1.8, forecast=15.0,
            daily_vol_pct=0.02, avg_book_correlation=0.95,
        )
        assert result_high_corr["correlation_adjusted"] <= result_low_corr["correlation_adjusted"]

    def test_all_values_are_floats(self):
        result = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.6,
            win_prob=0.55, win_loss_ratio=1.2, forecast=5.0,
            daily_vol_pct=0.015,
        )
        for v in result.values():
            assert isinstance(v, float)

    def test_custom_kelly_params(self):
        result = recommend_position_notional(
            capital_usd=10000.0, price=100.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=10.0,
            daily_vol_pct=0.02, kelly_multiplier=0.25, kelly_ceiling=0.15,
        )
        assert result["recommended"] >= 0
