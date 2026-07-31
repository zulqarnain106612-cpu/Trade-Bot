"""Tests for src/strategies/mean_reversion.py"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.strategies.mean_reversion import (
    MeanReversionSignal,
    OUParams,
    bollinger_signal,
    estimate_ou_params,
    is_mean_reverting,
    mean_reversion_signal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flat(n: int = 50, value: float = 100.0) -> np.ndarray:
    """Constant price series (no signal)."""
    return np.full(n, value, dtype=float)


def _oscillating(n: int = 60, amplitude: float = 2.0) -> np.ndarray:
    """Sine-wave price around 100 — classic mean-reverting series."""
    t = np.arange(n, dtype=float)
    return 100.0 + amplitude * np.sin(2 * np.pi * t / 20.0)


def _trending(n: int = 60, slope: float = 1.0) -> np.ndarray:
    return np.arange(n, dtype=float) * slope + 100.0


# ---------------------------------------------------------------------------
# bollinger_signal
# ---------------------------------------------------------------------------


def test_bollinger_insufficient_bars():
    prices = np.array([100.0, 101.0])
    sig = bollinger_signal(prices, lookback=20)
    assert sig.direction == 0
    assert "insufficient_bars" in sig.reject_reason


def test_bollinger_flat_series_std_too_small():
    sig = bollinger_signal(_flat(50), lookback=20)
    # std = 0 → reject
    assert sig.direction == 0
    assert "std_too_small" in sig.reject_reason


def test_bollinger_price_above_mean_signal_short():
    # mean≈100.5, std≈2.21, z≈4.3 >> entry_z=2 → always fires short entry
    prices = np.concatenate([np.full(19, 100.0), [110.0]])
    sig = bollinger_signal(prices, lookback=20, entry_z=2.0)
    assert sig.is_entry is True
    assert sig.direction == -1


def test_bollinger_price_below_mean_signal_long():
    # mean≈99.5, std≈2.21, z≈-4.3 << -entry_z=2 → always fires long entry
    prices = np.concatenate([np.full(19, 100.0), [90.0]])
    sig = bollinger_signal(prices, lookback=20, entry_z=2.0)
    assert sig.is_entry is True
    assert sig.direction == 1


def test_bollinger_z_score_finite():
    prices = np.random.default_rng(42).standard_normal(50) * 2.0 + 100.0
    sig = bollinger_signal(prices, lookback=20)
    assert math.isfinite(sig.z_score)


def test_bollinger_confidence_in_range():
    prices = np.concatenate([np.full(19, 100.0), [106.0]])
    sig = bollinger_signal(prices, lookback=20)
    assert 0.0 <= sig.confidence <= 1.0


def test_bollinger_exit_signal_when_z_near_zero():
    # 19 bars at 100, one at 102 (to give std > 0), then final at 100.
    # Window mean = (18*100 + 102 + 100)/20 = 100.1, std ≈ 0.447
    # z = (100 - 100.1) / 0.447 ≈ -0.224 < exit_z=0.5 → is_exit=True
    prices = np.concatenate([np.full(18, 100.0), [102.0], [100.0]])
    sig = bollinger_signal(prices, lookback=20, entry_z=2.0, exit_z=0.5)
    # Verify the precondition holds before asserting the exit flag
    assert abs(sig.z_score) < 0.5, f"precondition: |z|={abs(sig.z_score):.4f} must be < exit_z=0.5"
    assert sig.is_exit is True
    assert sig.is_entry is False


def test_bollinger_uses_most_recent_window():
    # First 30 bars far from 100; last 20 bars all at 100
    prices = np.concatenate([np.full(30, 50.0), np.full(20, 100.0)])
    sig = bollinger_signal(prices, lookback=20, entry_z=2.0)
    # std of last 20 bars = 0 → std_too_small
    assert "std_too_small" in sig.reject_reason


# ---------------------------------------------------------------------------
# estimate_ou_params
# ---------------------------------------------------------------------------


def test_ou_none_for_short_series():
    assert estimate_ou_params(np.array([1.0, 2.0, 3.0])) is None


def test_ou_none_for_trending_series():
    # Strongly trending: b > 0
    ou = estimate_ou_params(_trending(100, slope=5.0))
    assert ou is None  # trending, not mean-reverting


def test_ou_params_for_oscillating_series():
    prices = _oscillating(200)
    ou = estimate_ou_params(prices)
    assert ou is not None
    assert ou.theta > 0.0
    assert math.isfinite(ou.half_life_bars)
    assert ou.half_life_bars > 0.0


def test_ou_params_r_squared_in_range():
    prices = _oscillating(200)
    ou = estimate_ou_params(prices)
    assert ou is not None
    assert 0.0 <= ou.r_squared <= 1.0


def test_ou_params_sigma_nonnegative():
    prices = _oscillating(100)
    ou = estimate_ou_params(prices)
    assert ou is not None
    assert ou.sigma >= 0.0


# ---------------------------------------------------------------------------
# is_mean_reverting
# ---------------------------------------------------------------------------


def test_is_mean_reverting_oscillating():
    prices = _oscillating(200)
    ok, ou, reason = is_mean_reverting(prices, min_half_life=2, max_half_life=200)
    assert ok is True
    assert reason == ""
    assert ou is not None


def test_is_mean_reverting_trending_false():
    ok, ou, reason = is_mean_reverting(_trending(100), min_half_life=2, max_half_life=120)
    assert ok is False


def test_is_mean_reverting_half_life_too_short():
    # 4-bar sine period → AR(1) fit gives half-life ~1 bar, well below min=10
    prices = 100.0 + 2.0 * np.sin(2 * np.pi * np.arange(200) / 4.0)
    ok, ou, reason = is_mean_reverting(prices, min_half_life=10, max_half_life=9999)
    assert ok is False
    assert ou is not None  # OU fit must succeed for a sine wave
    assert ou.half_life_bars < 10
    assert "min" in reason


def test_is_mean_reverting_half_life_too_long():
    prices = _oscillating(200)
    ok, ou, reason = is_mean_reverting(prices, min_half_life=2, max_half_life=1)
    assert ou is not None  # oscillating series always yields an OU fit
    assert ou.half_life_bars > 1
    assert ok is False
    assert "max" in reason


# ---------------------------------------------------------------------------
# mean_reversion_signal — combined
# ---------------------------------------------------------------------------


def test_mean_reversion_signal_no_ou_gate():
    prices = np.concatenate([np.full(19, 100.0), [106.0]])
    sig = mean_reversion_signal(prices, lookback=20, entry_z=1.5, require_ou=False)
    assert isinstance(sig, MeanReversionSignal)


def test_mean_reversion_signal_trending_rejected_with_ou():
    # Trending series: OU gate should reject any entry even at low entry_z
    prices = _trending(100)
    sig = mean_reversion_signal(prices, lookback=20, entry_z=0.1, require_ou=True)
    # OU gate rejects trending series → no entry signal allowed
    assert sig.is_entry is False
    assert sig.direction == 0


def test_mean_reversion_signal_passes_without_entry():
    # If Bollinger says no entry, OU gate is not even checked
    prices = _flat(50)
    sig = mean_reversion_signal(prices, lookback=20, require_ou=True)
    assert sig.direction == 0


# ---------------------------------------------------------------------------
# MeanReversionSignal contract
# ---------------------------------------------------------------------------


def test_signal_frozen():
    prices = np.concatenate([np.full(19, 100.0), [105.0]])
    sig = bollinger_signal(prices, lookback=20)
    with pytest.raises((AttributeError, TypeError)):
        sig.direction = 1  # type: ignore[misc]


def test_signal_direction_is_valid():
    for val in [90.0, 95.0, 100.0, 105.0, 110.0]:
        prices = np.concatenate([np.full(19, 100.0), [val]])
        sig = bollinger_signal(prices, lookback=20)
        assert sig.direction in (-1, 0, 1)


def test_ou_params_frozen():
    ou = OUParams(theta=0.1, mu=100.0, sigma=1.0, half_life_bars=6.93, r_squared=0.8)
    with pytest.raises((AttributeError, TypeError)):
        ou.theta = 0.5  # type: ignore[misc]
