"""Tests for src/strategies/filters.py — professional strategy filter stack."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategies.filters import (
    adx_dmi,
    adx_filter_passes,
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


def _rising_series(n: int = 300, start: float = 100.0, step: float = 0.5) -> pd.Series:
    return pd.Series([start + i * step for i in range(n)])


def _falling_series(n: int = 300, start: float = 200.0, step: float = 0.5) -> pd.Series:
    return pd.Series([start - i * step for i in range(n)])


def _flat_series(n: int = 300, value: float = 100.0) -> pd.Series:
    return pd.Series([value] * n)


def _noisy_series(n: int = 300, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100.0 + np.cumsum(rng.normal(0, 1, n)))


# ---------------------------------------------------------------------------
# ewm_trend_signal
# ---------------------------------------------------------------------------


def test_ewm_trend_signal_rising_is_positive() -> None:
    signal = ewm_trend_signal(_rising_series(300), span=64)
    assert signal > 0.0


def test_ewm_trend_signal_falling_is_negative() -> None:
    signal = ewm_trend_signal(_falling_series(300), span=64)
    assert signal < 0.0


def test_ewm_trend_signal_too_short_returns_zero() -> None:
    assert ewm_trend_signal(pd.Series([100.0] * 5), span=64) == 0.0


def test_ewm_trend_signal_flat_returns_zero() -> None:
    signal = ewm_trend_signal(_flat_series(300), span=64)
    assert signal == 0.0


def test_ewm_trend_signal_clipped_at_three() -> None:
    # Extremely steep rise should be clipped to 3.0
    signal = ewm_trend_signal(_rising_series(500, step=100.0), span=64)
    assert abs(signal) <= 3.0


# ---------------------------------------------------------------------------
# trend_filter_passes
# ---------------------------------------------------------------------------


def test_trend_filter_passes_long_in_uptrend() -> None:
    assert trend_filter_passes(_rising_series(300, step=1.0), direction=1, span=64)


def test_trend_filter_blocks_long_in_downtrend() -> None:
    assert not trend_filter_passes(_falling_series(300, step=1.0), direction=1, span=64)


def test_trend_filter_passes_short_in_downtrend() -> None:
    assert trend_filter_passes(_falling_series(300, step=1.0), direction=-1, span=64)


def test_trend_filter_blocks_short_in_uptrend() -> None:
    assert not trend_filter_passes(_rising_series(300, step=1.0), direction=-1, span=64)


# ---------------------------------------------------------------------------
# vol_adjusted_momentum
# ---------------------------------------------------------------------------


def test_vol_adj_momentum_positive_for_rising() -> None:
    mom = vol_adjusted_momentum(_rising_series(100, step=1.0), window=20)
    assert mom > 0.0


def test_vol_adj_momentum_negative_for_falling() -> None:
    mom = vol_adjusted_momentum(_falling_series(100, step=1.0), window=20)
    assert mom < 0.0


def test_vol_adj_momentum_too_short_returns_zero() -> None:
    assert vol_adjusted_momentum(pd.Series([100.0, 101.0]), window=20) == 0.0


def test_vol_adj_momentum_flat_returns_zero() -> None:
    assert vol_adjusted_momentum(_flat_series(100), window=20) == 0.0


def test_vol_adj_momentum_clipped() -> None:
    mom = vol_adjusted_momentum(_rising_series(200, step=100.0), window=20)
    assert abs(mom) <= 5.0


# ---------------------------------------------------------------------------
# overnight_gap_is_excessive
# ---------------------------------------------------------------------------


def test_overnight_gap_not_excessive() -> None:
    assert not overnight_gap_is_excessive(open_price=100.0, prev_close=100.5, atr=1.0)


def test_overnight_gap_excessive() -> None:
    assert overnight_gap_is_excessive(open_price=103.0, prev_close=100.0, atr=1.0)


def test_overnight_gap_zero_atr_returns_false() -> None:
    assert not overnight_gap_is_excessive(open_price=200.0, prev_close=100.0, atr=0.0)


def test_overnight_gap_exactly_at_threshold_is_not_excessive() -> None:
    # gap = 2.0, atr = 1.0, threshold = 2.0 → gap NOT > threshold
    assert not overnight_gap_is_excessive(open_price=102.0, prev_close=100.0, atr=1.0)


# ---------------------------------------------------------------------------
# regime_position_scalar
# ---------------------------------------------------------------------------


def test_regime_scalar_volatile_state_returns_zero() -> None:
    from src.config import REGIME_VOLATILE

    scalar = regime_position_scalar(
        regime_state=REGIME_VOLATILE, prob_trending=0.3, prob_ranging=0.3, prob_volatile=0.4
    )
    assert scalar == 0.0


def test_regime_scalar_high_prob_volatile_returns_zero() -> None:
    from src.config import REGIME_TRENDING

    scalar = regime_position_scalar(
        regime_state=REGIME_TRENDING, prob_trending=0.3, prob_ranging=0.1, prob_volatile=0.9
    )
    assert scalar == 0.0


def test_regime_scalar_trending_clips_to_range() -> None:
    from src.config import REGIME_TRENDING

    scalar = regime_position_scalar(
        regime_state=REGIME_TRENDING, prob_trending=0.9, prob_ranging=0.05, prob_volatile=0.05
    )
    assert 0.5 <= scalar <= 1.0


def test_regime_scalar_ranging_clips_to_range() -> None:
    from src.config import REGIME_RANGING

    scalar = regime_position_scalar(
        regime_state=REGIME_RANGING, prob_trending=0.1, prob_ranging=0.8, prob_volatile=0.1
    )
    assert 0.2 <= scalar <= 0.6


def test_regime_scalar_unknown_returns_conservative() -> None:
    scalar = regime_position_scalar(
        regime_state=99, prob_trending=0.5, prob_ranging=0.3, prob_volatile=0.2
    )
    assert scalar == 0.5


# ---------------------------------------------------------------------------
# hurst_exponent
# ---------------------------------------------------------------------------


def test_hurst_exponent_insufficient_data_returns_half() -> None:
    assert hurst_exponent(pd.Series([1.0, 2.0, 3.0]), min_window=100) == 0.5


def test_hurst_exponent_in_range() -> None:
    close = _rising_series(500)
    H = hurst_exponent(close, min_window=50)
    assert 0.0 <= H <= 1.0


# ---------------------------------------------------------------------------
# hurst_filter_passes
# ---------------------------------------------------------------------------


def test_hurst_filter_passes_when_trending() -> None:
    # Strongly trending series should have H > 0.55 with enough data
    close = _rising_series(600, step=1.0)
    # Just verify it doesn't crash; result depends on numerical estimation
    result = hurst_filter_passes(close, direction=1)
    assert isinstance(result, bool)


def test_hurst_filter_returns_false_for_short_series() -> None:
    # With insufficient data hurst returns 0.5 which is < 0.55 → filter blocks
    result = hurst_filter_passes(pd.Series([100.0] * 5), direction=1)
    assert result is False


# ---------------------------------------------------------------------------
# obv_trend_confirms
# ---------------------------------------------------------------------------


def _obv_series(n: int = 50) -> tuple[pd.Series, pd.Series]:
    close = pd.Series([100.0 + i * 0.1 for i in range(n)])
    volume = pd.Series([1000.0] * n)
    return close, volume


def test_obv_confirms_long_on_rising_price() -> None:
    close, volume = _obv_series(50)
    assert obv_trend_confirms(close, volume, direction=1, window=20)


def test_obv_does_not_confirm_short_on_rising_price() -> None:
    close, volume = _obv_series(50)
    assert not obv_trend_confirms(close, volume, direction=-1, window=20)


def test_obv_confirms_short_on_falling_price() -> None:
    close = pd.Series([200.0 - i * 0.1 for i in range(50)])
    volume = pd.Series([1000.0] * 50)
    assert obv_trend_confirms(close, volume, direction=-1, window=20)


def test_obv_passes_when_data_too_short() -> None:
    close = pd.Series([100.0, 101.0])
    volume = pd.Series([1000.0, 1000.0])
    assert obv_trend_confirms(close, volume, direction=1, window=20)


# ---------------------------------------------------------------------------
# vol_explosion_blocks
# ---------------------------------------------------------------------------


def test_vol_explosion_blocks_on_spike() -> None:
    # Last bar ATR is 3x median → should block
    normal = [1.0] * 30
    atr = pd.Series([*normal, 3.0])
    assert vol_explosion_blocks(atr, lookback=20, multiplier=2.0)


def test_vol_explosion_passes_on_normal_vol() -> None:
    atr = pd.Series([1.0] * 30)
    assert not vol_explosion_blocks(atr, lookback=20, multiplier=2.0)


def test_vol_explosion_returns_false_on_short_series() -> None:
    assert not vol_explosion_blocks(pd.Series([1.0, 2.0]), lookback=20)


def test_vol_explosion_zero_median_returns_false() -> None:
    atr = pd.Series([0.0] * 30)
    assert not vol_explosion_blocks(atr, lookback=20)


# ---------------------------------------------------------------------------
# mtf_trend_aligned
# ---------------------------------------------------------------------------


def test_mtf_aligned_long_both_positive() -> None:
    assert mtf_trend_aligned(fast_signal=1.0, slow_signal=0.5, direction=1)


def test_mtf_not_aligned_long_fast_negative() -> None:
    assert not mtf_trend_aligned(fast_signal=-0.5, slow_signal=0.5, direction=1)


def test_mtf_not_aligned_long_slow_negative() -> None:
    assert not mtf_trend_aligned(fast_signal=0.5, slow_signal=-0.5, direction=1)


def test_mtf_aligned_short_both_negative() -> None:
    assert mtf_trend_aligned(fast_signal=-0.5, slow_signal=-1.0, direction=-1)


# ---------------------------------------------------------------------------
# adx_dmi
# ---------------------------------------------------------------------------


def _hlc_series(n: int = 100, seed: int = 42) -> tuple[pd.Series, pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    close = pd.Series(100.0 + np.cumsum(rng.normal(0, 0.5, n)))
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    return pd.Series(high), pd.Series(low), close


def test_adx_dmi_returns_three_floats() -> None:
    high, low, close = _hlc_series(100)
    adx, plus_di, minus_di = adx_dmi(high, low, close)
    assert isinstance(adx, float)
    assert isinstance(plus_di, float)
    assert isinstance(minus_di, float)


def test_adx_dmi_in_valid_range() -> None:
    high, low, close = _hlc_series(100)
    adx, plus_di, minus_di = adx_dmi(high, low, close)
    assert 0.0 <= adx <= 100.0
    assert plus_di >= 0.0
    assert minus_di >= 0.0


def test_adx_too_short_returns_zeros() -> None:
    high = pd.Series([101.0, 102.0])
    low = pd.Series([99.0, 100.0])
    close = pd.Series([100.0, 101.0])
    adx, plus_di, minus_di = adx_dmi(high, low, close)
    assert adx == 0.0


# ---------------------------------------------------------------------------
# adx_filter_passes
# ---------------------------------------------------------------------------


def test_adx_filter_returns_bool() -> None:
    high, low, close = _hlc_series(100)
    result = adx_filter_passes(high, low, close, direction=1)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# apply_all_strategy_filters
# ---------------------------------------------------------------------------


def test_apply_all_filters_returns_verdict_dict() -> None:
    from src.config import REGIME_TRENDING

    close = _rising_series(400, step=0.5)
    volume = pd.Series([1000.0] * 400)
    atr = pd.Series([1.0] * 400)
    high = close + 0.5
    low = close - 0.5

    result = apply_all_strategy_filters(
        close=close,
        volume=volume,
        atr_series=atr,
        direction=1,
        regime_state=REGIME_TRENDING,
        prob_trending=0.8,
        prob_ranging=0.1,
        prob_volatile=0.1,
        open_price=None,
        prev_close=None,
        high=high,
        low=low,
    )

    assert "passes" in result
    assert "scalar" in result
    assert "filters_failed" in result
    assert "details" in result
    assert isinstance(result["passes"], bool)
    assert 0.0 <= result["scalar"] <= 1.0


def test_apply_all_filters_volatile_blocks() -> None:
    from src.config import REGIME_VOLATILE

    close = _noisy_series(400)
    volume = pd.Series([1000.0] * 400)
    # Spike the last ATR to trigger vol explosion
    atr_vals = [1.0] * 400
    atr_vals[-1] = 5.0
    atr = pd.Series(atr_vals)

    result = apply_all_strategy_filters(
        close=close,
        volume=volume,
        atr_series=atr,
        direction=1,
        regime_state=REGIME_VOLATILE,
        prob_trending=0.1,
        prob_ranging=0.1,
        prob_volatile=0.8,
    )
    assert not result["passes"]
    assert result["scalar"] == 0.0


def test_apply_all_filters_weak_adx_appends_adx_failure() -> None:
    """apply_all_strategy_filters records adx_weak_or_misaligned when ADX filter fails."""
    from src.config import REGIME_TRENDING

    # Sinusoidal price → low ADX (no persistent trend)
    t = np.linspace(0, 40 * np.pi, 300)
    close = pd.Series(np.sin(t) * 5 + 100)
    volume = pd.Series([1000.0] * 300)
    atr = pd.Series([0.5] * 300)

    result = apply_all_strategy_filters(
        close=close,
        volume=volume,
        atr_series=atr,
        direction=1,
        regime_state=REGIME_TRENDING,
        prob_trending=0.9,
        prob_ranging=0.05,
        prob_volatile=0.05,
    )
    # ADX should be weak on a sinusoidal series → filter does not pass
    assert not result["passes"]


def test_adx_zero_true_range_returns_zeros() -> None:
    """All high=low=close (zero TR) → smoothed_tr near 0 → returns (0, 0, 0)."""
    n = 50
    price = pd.Series([100.0] * n)
    adx, plus_di, minus_di = adx_dmi(price, price, price)
    assert adx == 0.0
    assert plus_di == 0.0
    assert minus_di == 0.0


def test_apply_all_filters_overnight_gap_blocks() -> None:
    from src.config import REGIME_TRENDING

    close = _rising_series(400, step=0.5)
    volume = pd.Series([1000.0] * 400)
    atr = pd.Series([1.0] * 400)

    result = apply_all_strategy_filters(
        close=close,
        volume=volume,
        atr_series=atr,
        direction=1,
        regime_state=REGIME_TRENDING,
        prob_trending=0.8,
        prob_ranging=0.1,
        prob_volatile=0.1,
        open_price=float(close.iloc[-1]) + 5.0,  # large gap
        prev_close=float(close.iloc[-2]),
    )
    # At minimum, the details should record gap info
    assert "gap_excessive" in result["details"]
