"""Test coverage for src/strategies/position_sizing.py — Carver/AFML/Thorp sizing."""

import math

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
    """Carver (2019) Ch.4 forecast-scaled position sizing."""

    def test_positive_forecast_gives_positive_notional(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional > 0

    def test_zero_forecast_gives_zero_notional(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=0.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_zero_capital_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=0, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_negative_capital_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=-1000, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional == 0.0

    def test_zero_daily_vol_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=10.0, daily_vol_pct=0.0, price=100.0,
        )
        assert notional == 0.0

    def test_zero_price_returns_zero(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=10.0, daily_vol_pct=0.02, price=0.0,
        )
        assert notional == 0.0

    def test_forecast_clipped_to_max(self):
        notional_clipped = carver_forecast_position(
            capital_usd=100_000, forecast=100.0, daily_vol_pct=0.02, price=100.0,
        )
        notional_at_max = carver_forecast_position(
            capital_usd=100_000, forecast=20.0, daily_vol_pct=0.02, price=100.0,
        )
        assert notional_clipped == pytest.approx(notional_at_max)

    def test_negative_forecast_uses_absolute_value(self):
        pos_notional = carver_forecast_position(
            capital_usd=100_000, forecast=10.0, daily_vol_pct=0.02, price=100.0,
        )
        neg_notional = carver_forecast_position(
            capital_usd=100_000, forecast=-10.0, daily_vol_pct=0.02, price=100.0,
        )
        assert pos_notional == pytest.approx(neg_notional)
        assert neg_notional >= 0.0

    def test_capped_at_25_percent_of_capital(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=20.0, daily_vol_pct=0.0001, price=100.0,
        )
        assert notional <= 25_000.0

    def test_custom_forecast_scalar(self):
        notional = carver_forecast_position(
            capital_usd=100_000, forecast=10.0, daily_vol_pct=0.02, price=100.0,
            forecast_scalar=5.0,
        )
        assert isinstance(notional, float)
        assert notional >= 0.0


class TestVolTargetQuantity:
    """Carver (2019) Ch.2 volatility targeting."""

    def test_basic_quantity_positive(self):
        qty = vol_target_quantity(capital_usd=100_000, price=50.0, daily_vol_pct=0.02)
        assert qty > 0

    def test_zero_capital_returns_zero(self):
        qty = vol_target_quantity(capital_usd=0, price=50.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_negative_capital_returns_zero(self):
        qty = vol_target_quantity(capital_usd=-100, price=50.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_zero_price_returns_zero(self):
        qty = vol_target_quantity(capital_usd=100_000, price=0.0, daily_vol_pct=0.02)
        assert qty == 0.0

    def test_zero_vol_returns_zero(self):
        qty = vol_target_quantity(capital_usd=100_000, price=50.0, daily_vol_pct=0.0)
        assert qty == 0.0

    def test_higher_vol_gives_smaller_quantity(self):
        qty_low_vol = vol_target_quantity(capital_usd=100_000, price=50.0, daily_vol_pct=0.01)
        qty_high_vol = vol_target_quantity(capital_usd=100_000, price=50.0, daily_vol_pct=0.05)
        assert qty_low_vol > qty_high_vol

    def test_custom_vol_target(self):
        qty = vol_target_quantity(
            capital_usd=100_000, price=50.0, daily_vol_pct=0.02,
            daily_vol_target_pct=0.5,
        )
        assert qty > 0


class TestEstimateDailyVol:
    """EWM std of log returns volatility estimator."""

    def test_trending_series_positive_vol(self):
        close = np.linspace(100, 150, 100)
        vol = estimate_daily_vol(close)
        assert vol > 0

    def test_flat_series_low_vol(self):
        close = [100.0] * 50
        vol = estimate_daily_vol(close)
        assert vol >= 1e-6

    def test_volatile_series_higher_vol(self):
        np.random.seed(1)
        stable = 100 + np.cumsum(np.random.randn(100) * 0.1)
        volatile = 100 + np.cumsum(np.random.randn(100) * 5.0)
        vol_stable = estimate_daily_vol(stable)
        vol_volatile = estimate_daily_vol(volatile)
        assert vol_volatile > vol_stable

    def test_insufficient_data_returns_fallback(self):
        vol = estimate_daily_vol([100.0])
        assert vol == 0.01

    def test_two_points_minimal_data(self):
        vol = estimate_daily_vol([100.0, 101.0])
        assert vol == 0.01

    def test_list_input_accepted(self):
        vol = estimate_daily_vol([100.0, 101.0, 102.0, 103.0, 104.0])
        assert isinstance(vol, float)

    def test_custom_window(self):
        close = np.linspace(100, 110, 60)
        vol = estimate_daily_vol(close, window=40)
        assert vol > 0


class TestCorrelationAdjustedNotional:
    """AFML Ch.16 correlation-aware size reduction."""

    def test_low_correlation_no_reduction(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=10_000, avg_correlation_with_book=0.3,
        )
        assert notional == 10_000

    def test_at_threshold_no_reduction(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=10_000, avg_correlation_with_book=0.7,
        )
        assert notional == 10_000

    def test_high_correlation_reduces_notional(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=10_000, avg_correlation_with_book=0.85,
        )
        assert 0 < notional < 10_000

    def test_full_correlation_zero_notional(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=10_000, avg_correlation_with_book=1.0,
        )
        assert notional == pytest.approx(0.0, abs=1e-6)

    def test_custom_threshold(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=10_000, avg_correlation_with_book=0.5,
            threshold=0.4,
        )
        assert 0 < notional < 10_000

    def test_zero_proposed_notional(self):
        notional = correlation_adjusted_notional(
            proposed_notional_usd=0, avg_correlation_with_book=0.9,
        )
        assert notional == 0.0


class TestAfmlBetSize:
    """AFML Ch.10 bet sizing from model probability."""

    def test_no_edge_at_half_probability(self):
        size = afml_bet_size(p_long=0.5, capital_usd=100_000)
        assert size == 0.0

    def test_moderate_edge_positive_bet(self):
        """p=0.7 -> edge=0.4, but default max_fraction=0.25 caps it."""
        size = afml_bet_size(p_long=0.7, capital_usd=100_000)
        assert size == pytest.approx(25_000.0)

    def test_moderate_edge_uncapped(self):
        """p=0.7 -> edge=0.4, uncapped with higher max_fraction."""
        size = afml_bet_size(p_long=0.7, capital_usd=100_000, max_fraction=0.5)
        assert size == pytest.approx(40_000.0)

    def test_strong_edge_capped_at_max_fraction(self):
        size = afml_bet_size(p_long=0.9, capital_usd=100_000, max_fraction=0.25)
        assert size == pytest.approx(25_000.0)

    def test_below_half_probability_zero_bet(self):
        size = afml_bet_size(p_long=0.3, capital_usd=100_000)
        assert size == 0.0

    def test_p_equals_one_max_edge(self):
        size = afml_bet_size(p_long=1.0, capital_usd=100_000, max_fraction=0.5)
        assert size == pytest.approx(50_000.0)

    def test_p_equals_zero_zero_bet(self):
        size = afml_bet_size(p_long=0.0, capital_usd=100_000)
        assert size == 0.0

    def test_custom_max_fraction(self):
        size = afml_bet_size(p_long=0.95, capital_usd=100_000, max_fraction=0.1)
        assert size == pytest.approx(10_000.0)


class TestThorpKellyWithVariance:
    """Thorp (2006) fractional Kelly with variance penalty."""

    def test_positive_edge_gives_positive_size(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
        )
        assert notional > 0

    def test_no_edge_at_breakeven(self):
        b = 1.0
        p = 1.0 / (1.0 + b)
        notional = thorp_kelly_with_variance(
            win_prob=p, win_loss_ratio=b, capital_usd=100_000, price=50.0,
        )
        assert notional == pytest.approx(0.0, abs=1.0)

    def test_invalid_win_prob_zero_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.0, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
        )
        assert notional == 0.0

    def test_invalid_win_prob_one_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=1.0, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
        )
        assert notional == 0.0

    def test_invalid_win_loss_ratio_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=0.0, capital_usd=100_000, price=50.0,
        )
        assert notional == 0.0

    def test_zero_price_returns_zero(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=0.0,
        )
        assert notional == 0.0

    def test_variance_penalty_reduces_size(self):
        no_penalty = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
            variance_penalty=0.0,
        )
        with_penalty = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
            variance_penalty=0.4,
        )
        assert with_penalty < no_penalty

    def test_kelly_ceiling_caps_position(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.95, win_loss_ratio=5.0, capital_usd=100_000, price=50.0,
            kelly_ceiling=0.2,
        )
        assert notional <= 20_000.0 + 1e-6

    def test_custom_kelly_multiplier(self):
        full_kelly = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
            kelly_multiplier=1.0, kelly_ceiling=1.0,
        )
        half_kelly = thorp_kelly_with_variance(
            win_prob=0.6, win_loss_ratio=1.5, capital_usd=100_000, price=50.0,
            kelly_multiplier=0.5, kelly_ceiling=1.0,
        )
        assert half_kelly == pytest.approx(full_kelly * 0.5)

    def test_variance_penalty_clipped_at_half(self):
        notional = thorp_kelly_with_variance(
            win_prob=0.7, win_loss_ratio=2.0, capital_usd=100_000, price=50.0,
            variance_penalty=10.0,
        )
        assert notional >= 0.0


class TestRecommendPositionNotional:
    """Combined sizing recommendation — conservative minimum across methods."""

    def test_returns_all_method_keys(self):
        result = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=8.0,
            daily_vol_pct=0.02,
        )
        assert set(result.keys()) == {
            "thorp_kelly", "afml_bet_size", "carver_forecast",
            "correlation_adjusted", "recommended",
        }

    def test_recommended_is_minimum_of_methods(self):
        result = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=8.0,
            daily_vol_pct=0.02,
        )
        method_values = [
            result["thorp_kelly"], result["afml_bet_size"],
            result["carver_forecast"], result["correlation_adjusted"],
        ]
        assert result["recommended"] >= min(method_values) - 0.01
        assert result["recommended"] <= max(method_values) + 0.01

    def test_recommended_floored_at_minimum_notional(self):
        result = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.5,
            win_prob=0.5, win_loss_ratio=1.0, forecast=0.0,
            daily_vol_pct=0.02,
        )
        assert result["recommended"] >= 10.0

    def test_high_correlation_reduces_recommendation(self):
        low_corr = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.8,
            win_prob=0.65, win_loss_ratio=1.8, forecast=12.0,
            daily_vol_pct=0.02, avg_book_correlation=0.1,
        )
        high_corr = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.8,
            win_prob=0.65, win_loss_ratio=1.8, forecast=12.0,
            daily_vol_pct=0.02, avg_book_correlation=0.95,
        )
        assert high_corr["correlation_adjusted"] <= low_corr["correlation_adjusted"]

    def test_all_values_non_negative(self):
        result = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.3,
            win_prob=0.4, win_loss_ratio=0.8, forecast=-5.0,
            daily_vol_pct=0.03,
        )
        for v in result.values():
            assert v >= 0.0

    def test_custom_kelly_params(self):
        result = recommend_position_notional(
            capital_usd=100_000, price=50.0, p_long=0.7,
            win_prob=0.6, win_loss_ratio=1.5, forecast=8.0,
            daily_vol_pct=0.02, kelly_multiplier=0.25, kelly_ceiling=0.15,
        )
        assert result["thorp_kelly"] <= 100_000 * 0.15 + 0.01
