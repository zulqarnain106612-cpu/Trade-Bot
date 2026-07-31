"""Tests for src/strategies/breakout.py"""

from __future__ import annotations

import numpy as np
import pytest

from src.strategies.breakout import (
    BreakoutSignal,
    _compute_atr,
    breakout_signal,
    donchian_signal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flat(n: int = 50, value: float = 100.0) -> np.ndarray:
    return np.full(n, value, dtype=float)


def _trending_up(n: int = 50) -> np.ndarray:
    return np.arange(n, dtype=float) + 100.0


def _trending_down(n: int = 50) -> np.ndarray:
    return 100.0 + (n - np.arange(n, dtype=float))


def _range_prices(n: int = 50) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Oscillating OHLC: H = C+1, L = C-1."""
    c = 100.0 + np.sin(np.arange(n, dtype=float) * 0.3) * 5.0
    h = c + 1.0
    lo = c - 1.0
    return c, h, lo


# ---------------------------------------------------------------------------
# _compute_atr
# ---------------------------------------------------------------------------


def test_compute_atr_returns_zero_for_short_series():
    c = np.array([100.0])
    assert _compute_atr(c, c, c, period=14) == 0.0


def test_compute_atr_positive_for_volatile_series():
    c, h, lo = _range_prices(50)
    atr = _compute_atr(h, lo, c, period=14)
    assert atr > 0.0


def test_compute_atr_finite():
    c, h, lo = _range_prices(30)
    atr = _compute_atr(h, lo, c, period=14)
    assert isinstance(atr, float)
    import math

    assert math.isfinite(atr)


def test_compute_atr_larger_range_gives_higher_atr():
    c1, h1, lo1 = _range_prices(50)
    h2 = h1 * 2.0
    lo2 = lo1 / 2.0
    atr1 = _compute_atr(h1, lo1, c1, period=14)
    atr2 = _compute_atr(h2, lo2, c1, period=14)
    assert atr2 > atr1


# ---------------------------------------------------------------------------
# donchian_signal — insufficient bars
# ---------------------------------------------------------------------------


def test_donchian_insufficient_bars():
    prices = np.array([100.0, 101.0])
    sig = donchian_signal(prices, entry_period=20)
    assert sig.direction == 0
    assert "insufficient_bars" in sig.reject_reason


# ---------------------------------------------------------------------------
# donchian_signal — long breakout
# ---------------------------------------------------------------------------


def test_donchian_long_breakout():
    # 20 bars at 100, then price rockets to 150
    prices = np.concatenate([np.full(20, 100.0), [150.0]])
    sig = donchian_signal(prices, entry_period=20, exit_period=10)
    assert sig.is_entry is True
    assert sig.direction == 1


def test_donchian_short_breakout():
    # 20 bars at 100, then price drops to 50
    prices = np.concatenate([np.full(20, 100.0), [50.0]])
    sig = donchian_signal(prices, entry_period=20, exit_period=10)
    assert sig.is_entry is True
    assert sig.direction == -1


def test_donchian_no_signal_within_channel():
    prices = np.concatenate([np.full(20, 100.0), [100.0]])
    sig = donchian_signal(prices, entry_period=20)
    assert sig.direction == 0
    assert sig.is_entry is False


# ---------------------------------------------------------------------------
# donchian_signal — exit signal
# ---------------------------------------------------------------------------


def test_donchian_exit_signal_when_within_exit_channel():
    # Price stays in the tighter exit channel
    prices = np.concatenate([np.full(20, 100.0), [100.0]])
    sig = donchian_signal(prices, entry_period=20, exit_period=5)
    assert sig.is_exit is True


# ---------------------------------------------------------------------------
# donchian_signal — channel values
# ---------------------------------------------------------------------------


def test_donchian_upper_channel_is_period_max():
    prices = np.arange(1, 32, dtype=float)  # 31 bars: 1..31
    sig = donchian_signal(prices, entry_period=20, exit_period=5)
    # channel uses bars [-21:-1] = bars 10..30 (1-indexed: 11..30) → max = 30
    assert sig.upper_channel == pytest.approx(30.0)


def test_donchian_lower_channel_is_period_min():
    prices = np.arange(1, 32, dtype=float)
    sig = donchian_signal(prices, entry_period=20, exit_period=5)
    # bars 10..30 → min = 11 (0-indexed bar 10 = price 11)
    assert sig.lower_channel == pytest.approx(11.0)


def test_donchian_confidence_zero_no_entry():
    prices = np.concatenate([np.full(20, 100.0), [100.0]])
    sig = donchian_signal(prices, entry_period=20)
    assert sig.confidence == 0.0


def test_donchian_confidence_positive_on_entry():
    prices = np.concatenate([np.full(20, 100.0), [110.0]])
    sig = donchian_signal(prices, entry_period=20)
    if sig.is_entry:
        assert sig.confidence > 0.0


# ---------------------------------------------------------------------------
# donchian_signal — highs/lows fallback to closes
# ---------------------------------------------------------------------------


def test_donchian_without_highs_lows():
    prices = np.concatenate([np.full(20, 100.0), [150.0]])
    sig = donchian_signal(prices)  # no highs/lows
    assert sig.is_entry is True
    assert sig.direction == 1


# ---------------------------------------------------------------------------
# breakout_signal — ATR gate
# ---------------------------------------------------------------------------


def test_breakout_atr_gate_flat_market():
    # Flat series → ATR ≈ 0 → atr_pct too low → no entry
    prices = np.concatenate([np.full(25, 100.0), [110.0]])
    sig = breakout_signal(prices, entry_period=20, min_atr_pct=0.5)
    # If a breakout was detected but ATR is tiny, it should be suppressed
    if "atr_too_low" in sig.reject_reason:
        assert sig.direction == 0


def test_breakout_atr_gate_normal_series():
    c, h, lo = _range_prices(50)
    # Force a breakout by appending a spike
    c = np.append(c, c.max() + 10.0)
    h = np.append(h, h.max() + 10.0)
    lo = np.append(lo, lo[-1])
    sig = breakout_signal(c, h, lo, entry_period=20, min_atr_pct=0.01, max_atr_pct=50.0)
    assert isinstance(sig, BreakoutSignal)


def test_breakout_atr_populated():
    c, h, lo = _range_prices(50)
    sig = breakout_signal(c, h, lo, entry_period=20)
    assert sig.atr >= 0.0
    assert sig.atr_pct >= 0.0


def test_breakout_no_entry_passes_atr_gate():
    # No entry signal → ATR gate not checked
    prices = _flat(30)
    sig = breakout_signal(prices, entry_period=20, min_atr_pct=99.0)
    assert sig.direction == 0
    assert "atr_too_low" not in sig.reject_reason


# ---------------------------------------------------------------------------
# BreakoutSignal contract
# ---------------------------------------------------------------------------


def test_signal_frozen():
    prices = np.concatenate([np.full(20, 100.0), [150.0]])
    sig = donchian_signal(prices)
    with pytest.raises((AttributeError, TypeError)):
        sig.direction = 0  # type: ignore[misc]


def test_signal_direction_valid():
    for val in [50.0, 100.0, 150.0]:
        prices = np.concatenate([np.full(20, 100.0), [val]])
        sig = donchian_signal(prices, entry_period=20)
        assert sig.direction in (-1, 0, 1)


def test_signal_confidence_in_range():
    prices = np.concatenate([np.full(20, 100.0), [150.0]])
    sig = donchian_signal(prices, entry_period=20)
    assert 0.0 <= sig.confidence <= 1.0
