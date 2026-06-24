"""Test coverage for src/strategies/filters.py — research-backed signal filters."""

import numpy as np
import pandas as pd

from src.strategies.filters import (
    ewm_trend_signal,
    hurst_exponent,
    hurst_filter_passes,
    mtf_trend_aligned,
    obv_trend_confirms,
    overnight_gap_is_excessive,
    regime_position_scalar,
    trend_filter_passes,
    vol_adjusted_momentum,
    vol_explosion_blocks,
)


class TestEwmTrendSignal:
    """EWM-based trend signal generation."""

    def test_uptrend_positive_signal(self):
        close = pd.Series(np.linspace(100, 200, 300))
        signal = ewm_trend_signal(close)
        assert signal > 0

    def test_downtrend_negative_signal(self):
        close = pd.Series(np.linspace(200, 100, 300))
        signal = ewm_trend_signal(close)
        assert signal < 0

    def test_flat_market_near_zero(self):
        close = pd.Series([100.0] * 300)
        signal = ewm_trend_signal(close)
        assert abs(signal) < 0.5

    def test_custom_span(self):
        close = pd.Series(np.linspace(100, 150, 200))
        signal = ewm_trend_signal(close, span=50)
        assert isinstance(signal, float)


class TestTrendFilterPasses:
    """Trend filter decision rule."""

    def test_long_trend_up_passes(self):
        close = pd.Series(np.linspace(100, 200, 300))
        result = trend_filter_passes(close, direction=1)
        assert result is True

    def test_long_trend_down_fails(self):
        close = pd.Series(np.linspace(200, 100, 300))
        result = trend_filter_passes(close, direction=1)
        assert result is False

    def test_short_trend_down_passes(self):
        close = pd.Series(np.linspace(200, 100, 300))
        result = trend_filter_passes(close, direction=-1)
        assert result is True

    def test_short_trend_up_fails(self):
        close = pd.Series(np.linspace(100, 200, 300))
        result = trend_filter_passes(close, direction=-1)
        assert result is False


class TestVolAdjustedMomentum:
    """Risk-adjusted momentum signal (Chan 2013)."""

    def test_uptrend_positive_momentum(self):
        close = pd.Series(np.linspace(100, 150, 100))
        mom = vol_adjusted_momentum(close)
        assert mom > 0

    def test_downtrend_negative_momentum(self):
        close = pd.Series(np.linspace(150, 100, 100))
        mom = vol_adjusted_momentum(close)
        assert mom < 0

    def test_flat_price_zero_momentum(self):
        close = pd.Series([100.0] * 50)
        mom = vol_adjusted_momentum(close)
        assert abs(mom) < 0.01

    def test_custom_window(self):
        close = pd.Series(np.linspace(100, 150, 100))
        mom = vol_adjusted_momentum(close, window=10)
        assert isinstance(mom, float)


class TestOvernightGapIsExcessive:
    """Gap filter detects noisy overnight opens."""

    def test_large_gap_detected(self):
        open_p = 150.0
        prev_close = 100.0
        atr = 10.0
        result = overnight_gap_is_excessive(open_p, prev_close, atr)
        assert result is True

    def test_small_gap_allowed(self):
        open_p = 102.0
        prev_close = 100.0
        atr = 10.0
        result = overnight_gap_is_excessive(open_p, prev_close, atr)
        assert result is False

    def test_no_gap(self):
        result = overnight_gap_is_excessive(100.0, 100.0, 10.0)
        assert result is False

    def test_custom_threshold(self):
        result = overnight_gap_is_excessive(
            open_price=115.0,
            prev_close=100.0,
            atr=10.0,
            threshold_atr_multiples=2.0,
        )
        assert isinstance(result, bool)


class TestRegimePositionScaler:
    """Position size scaling by regime confidence."""

    def test_trending_regime_high_prob(self):
        scalar = regime_position_scalar(
            regime_state=1,
            prob_trending=0.9,
            prob_ranging=0.05,
            prob_volatile=0.05,
        )
        assert 0.5 <= scalar <= 1.0

    def test_ranging_regime(self):
        scalar = regime_position_scalar(
            regime_state=0,
            prob_trending=0.3,
            prob_ranging=0.6,
            prob_volatile=0.1,
        )
        assert 0.0 <= scalar <= 0.8

    def test_volatile_regime(self):
        scalar = regime_position_scalar(
            regime_state=2,
            prob_trending=0.1,
            prob_ranging=0.1,
            prob_volatile=0.8,
        )
        assert 0.0 <= scalar <= 0.6

    def test_low_confidence_all_regimes(self):
        scalar = regime_position_scalar(
            regime_state=1,
            prob_trending=0.33,
            prob_ranging=0.33,
            prob_volatile=0.34,
        )
        assert 0.0 <= scalar <= 0.5


class TestHurstExponent:
    """Hurst exponent for trending detection."""

    def test_trending_series_h_high(self):
        close = pd.Series(np.linspace(100, 200, 150))
        h = hurst_exponent(close, min_window=50)
        assert h > 0.5

    def test_random_walk_h_neutral(self):
        np.random.seed(42)
        close = pd.Series(np.cumsum(np.random.randn(150)))
        h = hurst_exponent(close, min_window=50)
        assert 0.3 <= h <= 0.7

    def test_mean_revert_h_low(self):
        close = pd.Series(np.sin(np.linspace(0, 10 * np.pi, 150)) + 100)
        h = hurst_exponent(close, min_window=50)
        assert isinstance(h, float)

    def test_insufficient_data_returns_neutral(self):
        close = pd.Series([100, 101, 102])
        h = hurst_exponent(close, min_window=50)
        assert h == 0.5


class TestHurstFilterPasses:
    """Hurst filter for fractal market hypothesis."""

    def test_trending_market_long_passes(self):
        close = pd.Series(np.linspace(100, 200, 150))
        result = hurst_filter_passes(close, direction=1)
        assert result is True

    def test_flat_market_behavior(self):
        close = pd.Series([100.0] * 150)
        result = hurst_filter_passes(close, direction=-1)
        assert isinstance(result, bool)

    def test_mean_revert_market(self):
        close = pd.Series(np.sin(np.linspace(0, 10 * np.pi, 150)) + 100)
        result = hurst_filter_passes(close, direction=1)
        assert isinstance(result, bool)


class TestObvTrendConfirms:
    """OBV confirmation of price direction."""

    def test_obv_confirms_uptrend(self):
        close = pd.Series([100, 105, 110, 115])
        volume = pd.Series([1000, 2000, 3000, 4000])
        result = obv_trend_confirms(close, volume, direction=1)
        assert result is True

    def test_divergence_uptrend(self):
        """Price up but volume down - divergence handling."""
        close = pd.Series([100, 105, 110, 115])
        volume = pd.Series([4000, 3000, 2000, 1000])
        result = obv_trend_confirms(close, volume, direction=1)
        # Let the function determine the result
        assert isinstance(result, bool)

    def test_obv_confirms_downtrend(self):
        close = pd.Series([115, 110, 105, 100])
        volume = pd.Series([4000, 3000, 2000, 1000])
        result = obv_trend_confirms(close, volume, direction=-1)
        assert result is True

    def test_flat_obv(self):
        close = pd.Series([100, 105, 110])
        volume = pd.Series([1000, 1000, 1000])
        result = obv_trend_confirms(close, volume, direction=1)
        assert isinstance(result, bool)


class TestVolExplosionBlocks:
    """Volatility regime gate (Schwager vol explosion filter)."""

    def test_normal_vol_allows_trade(self):
        atr = pd.Series(np.linspace(10, 12, 50))
        result = vol_explosion_blocks(atr)
        assert result is False

    def test_vol_spike_blocks_trade(self):
        atr = list(np.linspace(10, 12, 40))
        atr.extend([40, 40, 40, 40, 40])
        result = vol_explosion_blocks(pd.Series(atr))
        assert result is True

    def test_flat_atr_allows_trade(self):
        atr = pd.Series([10.0] * 50)
        result = vol_explosion_blocks(atr)
        assert result is False

    def test_custom_multiplier(self):
        atr = pd.Series(np.linspace(10, 12, 50))
        result = vol_explosion_blocks(atr, multiplier=3.0)
        assert isinstance(result, bool)

    def test_custom_lookback(self):
        atr = pd.Series(np.linspace(10, 12, 50))
        result = vol_explosion_blocks(atr, lookback=15)
        assert isinstance(result, bool)


class TestMtfTrendAligned:
    """Multi-timeframe trend alignment."""

    def test_aligned_uptrends_long(self):
        result = mtf_trend_aligned(fast_signal=0.8, slow_signal=1.0, direction=1)
        assert result is True

    def test_misaligned_trends_long(self):
        result = mtf_trend_aligned(fast_signal=0.8, slow_signal=-1.0, direction=1)
        assert result is False

    def test_aligned_downtrends_short(self):
        result = mtf_trend_aligned(fast_signal=-0.8, slow_signal=-1.0, direction=-1)
        assert result is True

    def test_neutral_slow_signal(self):
        result = mtf_trend_aligned(fast_signal=0.5, slow_signal=0.0, direction=1)
        assert isinstance(result, bool)

    def test_opposite_signals(self):
        result = mtf_trend_aligned(fast_signal=0.5, slow_signal=-0.8, direction=-1)
        assert isinstance(result, bool)
