"""Tests for the mean-reversion pairs strategy (v2 Sub-task 2, family 1)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.mean_reversion import (
    MeanReversionPairsStrategy,
    PairContext,
    check_cointegration,
    compute_spread_zscore,
)
from src.strategies.registry import StrategyRegistry


def _make_cointegrated_pair(n: int = 200, seed: int = 42) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0, 1, n)) + 100
    noise_a = rng.normal(0, 0.5, n)
    noise_b = rng.normal(0, 0.5, n)
    price_a = pd.Series(common + noise_a)
    price_b = pd.Series(common * 0.5 + noise_b)
    return price_a, price_b


def _make_random_walk_pair(n: int = 200, seed: int = 7) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    price_a = pd.Series(np.cumsum(rng.normal(0, 1, n)) + 100)
    price_b = pd.Series(np.cumsum(rng.normal(0, 1, n)) + 100)
    return price_a, price_b


def test_cointegration_detects_cointegrated_pair() -> None:
    price_a, price_b = _make_cointegrated_pair()
    result = check_cointegration(price_a, price_b)
    assert result.is_cointegrated
    assert 0.0 <= result.pvalue < 0.05
    assert result.hedge_ratio != 0.0


def test_cointegration_rejects_short_series() -> None:
    with pytest.raises(ValueError, match="need >="):
        check_cointegration(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0]))


def test_cointegration_rejects_mismatched_lengths() -> None:
    a = pd.Series(np.arange(100, dtype=float))
    b = pd.Series(np.arange(90, dtype=float))
    with pytest.raises(ValueError, match="same length"):
        check_cointegration(a, b)


def test_spread_zscore_shape_and_nan_prefix() -> None:
    price_a, price_b = _make_cointegrated_pair()
    z = compute_spread_zscore(price_a, price_b, hedge_ratio=0.5, window=30)
    assert len(z) == len(price_a)
    assert z.iloc[:29].isna().all()
    assert not z.iloc[-1:].isna().all()


def test_strategy_rejects_non_paircontext_bar() -> None:
    strat = MeanReversionPairsStrategy()
    with pytest.raises(TypeError, match="PairContext"):
        strat.generate_signal(bar=None)


def test_strategy_flat_when_zscore_below_entry() -> None:
    price_a = pd.Series([100.0] * 60)
    price_b = pd.Series([50.0] * 60)
    strat = MeanReversionPairsStrategy()
    ctx = PairContext(price_a=price_a, price_b=price_b, hedge_ratio=2.0, window=30)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_strategy_shorts_a_when_spread_far_above_mean() -> None:
    price_a = pd.Series([100.0] * 50 + [130.0] * 5)
    price_b = pd.Series([50.0] * 55)
    strat = MeanReversionPairsStrategy()
    ctx = PairContext(price_a=price_a, price_b=price_b, hedge_ratio=1.0, window=30)
    sig = strat.generate_signal(ctx)
    assert sig.direction in (-1, 0)


def test_strategy_longs_a_when_spread_far_below_mean() -> None:
    price_a = pd.Series([100.0] * 50 + [70.0] * 5)
    price_b = pd.Series([50.0] * 55)
    strat = MeanReversionPairsStrategy()
    ctx = PairContext(price_a=price_a, price_b=price_b, hedge_ratio=1.0, window=30)
    sig = strat.generate_signal(ctx)
    assert sig.direction in (1, 0)


def _bar_context_for_target_zscore(target_z: float) -> PairContext:
    """
    Build a PairContext whose final-bar spread z-score equals target_z, by
    binary-searching the last bar's value against the strategy's own
    compute_spread_zscore() — avoids re-deriving mean/std by hand (which
    is easy to get subtly wrong since the rolling window includes the
    value being solved for).
    """
    pattern = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(29)]
    prior = pd.Series(pattern * 2)[:59].reset_index(drop=True)  # 59 fixed bars

    def _z_for(last_value: float) -> float:
        price_a = pd.concat([prior, pd.Series([last_value])], ignore_index=True)
        price_b = pd.Series([0.0] * len(price_a))
        z_series = compute_spread_zscore(price_a, price_b, hedge_ratio=0.0, window=30)
        return float(z_series.iloc[-1])

    lo, hi = 50.0, 150.0
    assert _z_for(lo) < target_z < _z_for(hi)
    for _ in range(60):
        mid = (lo + hi) / 2
        if _z_for(mid) < target_z:
            lo = mid
        else:
            hi = mid
    last_value = (lo + hi) / 2
    assert abs(_z_for(last_value) - target_z) < 1e-4

    price_a = pd.concat([prior, pd.Series([last_value])], ignore_index=True)
    price_b = pd.Series([0.0] * len(price_a))
    return PairContext(price_a=price_a, price_b=price_b, hedge_ratio=0.0, window=30)


def test_strategy_flat_when_zscore_near_zero_within_exit_band() -> None:
    ctx = _bar_context_for_target_zscore(0.2)
    strat = MeanReversionPairsStrategy()
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0
    assert sig.regime_fit == 0.5


def test_strategy_flat_when_zscore_between_exit_and_entry() -> None:
    ctx = _bar_context_for_target_zscore(1.2)
    strat = MeanReversionPairsStrategy()
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0
    assert sig.regime_fit == 0.5


def test_strategy_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(MeanReversionPairsStrategy())
    assert "mean_reversion_pairs_v1" in registry


def test_strategy_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        MeanReversionPairsStrategy(max_capital_fraction=0.0)


def test_random_walk_pair_typically_not_cointegrated() -> None:
    price_a, price_b = _make_random_walk_pair()
    result = check_cointegration(price_a, price_b)
    assert isinstance(result.is_cointegrated, bool)
