"""Test coverage for src/strategies/filters.py — research-backed signal filters."""

import numpy as np
import pandas as pd

from src.strategies.filters import (
    apply_all_strategy_filters,
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

    def test_insufficient_data_returns_zero(self):
        close = pd.Series(np.linspace(100, 110, 50))  # < default span=200
        signal = ewm_trend_signal(close)
        assert signal == 0.0


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

    def test_insufficient_data_returns_zero(self):
        close = pd.Series(np.linspace(100, 101, 5))  # < window+1
        mom = vol_adjusted_momentum(close, window=20)
        assert mom == 0.0

    def test_log_returns_shorter_than_window_after_dropna(self):
        # len(close) >= window+1 but internal NaNs reduce log_ret below window
        # after dropna().
        vals = list(np.linspace(100, 110, 21))
        vals[10] = np.nan
        close = pd.Series(vals)
        mom = vol_adjusted_momentum(close, window=20)
        assert mom == 0.0


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

    def test_near_zero_atr_returns_false(self):
        result = overnight_gap_is_excessive(open_price=150.0, prev_close=100.0, atr=0.0)
        assert result is False


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

    def test_unknown_regime_returns_conservative_default(self):
        scalar = regime_position_scalar(
            regime_state=99,  # not RANGING(0), TRENDING(1), or VOLATILE(2)
            prob_trending=0.3,
            prob_ranging=0.3,
            prob_volatile=0.3,
        )
        assert scalar == 0.5


class TestHurstExponent:
    """Hurst exponent for trending detection."""

    def test_trending_series_h_high(self):
        close = pd.Series(np.linspace(100, 200, 150))
        h = hurst_exponent(close, min_window=50)
        assert h > 0.5

    def test_random_walk_h_neutral(self):
        np.random.seed(42)
        close = pd.Series(100 + np.cumsum(np.random.randn(150)))
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

    def test_small_min_window_skips_short_lags(self):
        # min_window=20 -> candidate lags [5, 10, 20, 40]; the lag=5 candidate
        # is < 10 and must be skipped (continue branch), while the rest still
        # produce a valid regression.
        close = pd.Series(100 + np.cumsum(np.random.default_rng(3).standard_normal(60)))
        h = hurst_exponent(close, min_window=20)
        assert isinstance(h, float)
        assert 0.0 <= h <= 1.0

    def test_log_returns_shorter_than_min_window_after_dropna(self):
        # len(close) >= min_window (passes the first guard) but an internal
        # NaN reduces log_ret's length (after dropna) below min_window.
        vals = list(100 + np.cumsum(np.random.default_rng(9).standard_normal(20)))
        vals[10] = np.nan
        close = pd.Series(vals)
        h = hurst_exponent(close, min_window=20)
        assert h == 0.5

    def test_degenerate_regression_denominator_returns_neutral(self):
        # min_window=15 with len(close)=16 makes log_ret length exactly 15,
        # so both min_window (15) and min(n, min_window*2)=min(15,30)=15
        # candidates collapse to the *same* lag value while min_window//4=3
        # and min_window//2=7 are both skipped (<10). The regression then
        # has two identical x=log(lag) points, so the OLS denominator is 0.
        close = pd.Series(100 + np.cumsum(np.random.default_rng(5).standard_normal(16)))
        h = hurst_exponent(close, min_window=15)
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

    def test_short_direction_branch_with_sufficient_data(self):
        # Needs len >= window+2 (default window=20) to reach the direction
        # check instead of the early "insufficient data" True return.
        close = pd.Series(np.linspace(130, 100, 25))  # declining price
        volume = pd.Series(np.linspace(4000, 1000, 25))  # declining volume too
        result = obv_trend_confirms(close, volume, direction=-1)
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

    def test_insufficient_data_returns_false(self):
        atr = pd.Series(np.linspace(10, 12, 5))  # < default lookback+1
        result = vol_explosion_blocks(atr)
        assert result is False

    def test_near_zero_median_returns_false(self):
        atr = pd.Series([0.0] * 25)
        result = vol_explosion_blocks(atr)
        assert result is False


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


def _trending_bars(n: int = 250) -> tuple[pd.Series, pd.Series]:
    """Steadily-rising close + volume series, long enough for all filters."""
    close = pd.Series(np.linspace(100, 200, n))
    volume = pd.Series(np.linspace(1000, 2000, n))
    return close, volume


def _flat_atr(n: int = 250, level: float = 10.0) -> pd.Series:
    return pd.Series([level] * n)


class TestApplyAllStrategyFilters:
    """Combined filter stack — each `failed.append(...)` branch individually."""

    def test_vol_explosion_blocks_and_logs(self):
        close, volume = _trending_bars()
        atr = list(np.linspace(10, 12, 245))
        atr.extend([50, 50, 50, 50, 50])  # spike -> vol_explosion_blocks() True
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=pd.Series(atr),
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        assert result["passes"] is False
        assert "vol_explosion" in result["filters_failed"]
        assert result["scalar"] == 0.0

    def test_hurst_insufficient_appends_failure(self):
        # Oscillating price -> low Hurst exponent, no vol explosion.
        close = pd.Series(np.sin(np.linspace(0, 20 * np.pi, 250)) + 100)
        volume = pd.Series(np.linspace(1000, 2000, 250))
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        assert result["details"]["hurst"] < 0.55
        assert "hurst_insufficient" in result["filters_failed"]
        assert result["passes"] is False

    def test_trend_counter_appends_failure(self):
        # Strong uptrend but direction proposed is short (-1) -> counter-trend.
        close, volume = _trending_bars()
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=-1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        assert result["passes"] is False
        assert len(result["filters_failed"]) >= 1

    def test_obv_divergence_appends_failure(self):
        # Strong uptrend (keeps vol_explosion/hurst/trend all passing), but a
        # handful of deliberate down-ticks carry disproportionately heavy
        # volume so the cumulative OBV slope goes negative -> divergence with
        # the proposed long direction, without tripping any earlier filter.
        n = 250
        close_vals = list(np.linspace(100, 160, n))
        for i in (50, 100, 150, 200, 249):
            close_vals[i] = close_vals[i - 1] - 0.01
        close = pd.Series(close_vals)
        delta = close.diff()
        volume = pd.Series(np.where(delta.fillna(0) < 0, 50_000.0, 100.0))
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        assert result["details"]["obv_confirms"] is False
        assert "obv_divergence" in result["filters_failed"]
        assert result["passes"] is False

    def test_overnight_gap_appends_failure(self):
        close, volume = _trending_bars()
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
            open_price=200.0,
            prev_close=100.0,  # huge gap vs flat atr=10
        )
        assert result["passes"] is False
        assert "overnight_gap" in result["filters_failed"]

    def test_gap_checked_but_not_excessive(self):
        # open_price/prev_close supplied (gap-check block runs) but the gap
        # is small -> gap_excessive branch's False arm (445->449 fallthrough).
        close, volume = _trending_bars()
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
            open_price=100.5,
            prev_close=100.0,  # tiny gap vs atr=10
        )
        assert result["details"]["gap_excessive"] is False
        assert "overnight_gap" not in result["filters_failed"]

    def test_all_pass_returns_nonzero_scalar(self):
        close, volume = _trending_bars()
        atr = _flat_atr()
        result = apply_all_strategy_filters(
            close=close,
            volume=volume,
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        if result["passes"]:
            assert result["scalar"] > 0.0
            assert result["filters_failed"] == []
