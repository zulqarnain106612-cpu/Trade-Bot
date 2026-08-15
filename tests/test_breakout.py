"""Tests for the volume-weighted breakout strategy (v2 Sub-task 2, family 3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.breakout import BreakoutContext, BreakoutStrategy, compute_atr
from src.strategies.registry import StrategyRegistry


def _flat_range_context(
    n: int = 25, breakout_close: float | None = None, breakout_volume: float | None = None
) -> BreakoutContext:
    rng = np.random.default_rng(1)
    close = pd.Series(100 + rng.normal(0, 0.2, n))
    high = close + 0.5
    low = close - 0.5
    volume = pd.Series([1000.0] * n)

    if breakout_close is not None:
        close.iloc[-1] = breakout_close
        high.iloc[-1] = max(high.iloc[-1], breakout_close)
        low.iloc[-1] = min(low.iloc[-1], breakout_close)
    if breakout_volume is not None:
        volume.iloc[-1] = breakout_volume

    return BreakoutContext(high=high, low=low, close=close, volume=volume)


def test_compute_atr_matches_length_and_has_nan_prefix() -> None:
    n = 30
    close = pd.Series(np.linspace(100, 110, n))
    high = close + 1
    low = close - 1
    atr = compute_atr(high, low, close, period=14)
    assert len(atr) == n
    assert atr.iloc[:13].isna().all()
    assert not pd.isna(atr.iloc[-1])


def test_rejects_non_breakoutcontext_bar() -> None:
    strat = BreakoutStrategy()
    with pytest.raises(TypeError, match="BreakoutContext"):
        strat.generate_signal(bar=None)


def test_flat_with_insufficient_bars() -> None:
    strat = BreakoutStrategy()
    ctx = BreakoutContext(
        high=pd.Series([101.0] * 5),
        low=pd.Series([99.0] * 5),
        close=pd.Series([100.0] * 5),
        volume=pd.Series([1000.0] * 5),
    )
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_flat_without_volume_confirmation() -> None:
    strat = BreakoutStrategy()
    ctx = _flat_range_context(breakout_close=105.0, breakout_volume=1000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_long_on_upside_breakout_with_volume() -> None:
    strat = BreakoutStrategy()
    ctx = _flat_range_context(breakout_close=110.0, breakout_volume=3000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1
    assert sig.confidence > 0.0


def test_short_on_downside_breakout_with_volume() -> None:
    strat = BreakoutStrategy()
    ctx = _flat_range_context(breakout_close=90.0, breakout_volume=3000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1


def test_flat_when_range_bound_with_volume_spike() -> None:
    strat = BreakoutStrategy()
    ctx = _flat_range_context(breakout_close=100.1, breakout_volume=3000.0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(BreakoutStrategy())
    assert "breakout_volume_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        BreakoutStrategy(max_capital_fraction=0.0)
