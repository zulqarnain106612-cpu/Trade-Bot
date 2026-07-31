"""Tests for the macro-indicator producer that feeds the v7 macro overlay."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intelligence.macro_indicators import MIN_OBSERVATIONS, build_macro_indicators


def _frame(**columns: list[float]) -> pd.DataFrame:
    length = max(len(v) for v in columns.values())
    return pd.DataFrame(columns, index=pd.RangeIndex(length, name="bar_ts"))


def _rising(n: int = MIN_OBSERVATIONS + 4) -> list[float]:
    return [float(i) for i in range(n)]


def test_empty_frame_returns_none() -> None:
    assert build_macro_indicators(pd.DataFrame()) is None


def test_none_frame_returns_none() -> None:
    assert build_macro_indicators(None) is None  # type: ignore[arg-type]


def test_frame_without_any_known_column_returns_none() -> None:
    frame = _frame(intelligence_sopr=_rising())
    assert build_macro_indicators(frame) is None


def test_too_few_observations_returns_none() -> None:
    short = [0.1] * (MIN_OBSERVATIONS - 1)
    frame = _frame(
        intelligence_binance_funding_rate_pct=short,
        intelligence_stablecoin_reserve_ratio=short,
        intelligence_exchange_netflow_7d_zscore=short,
    )
    assert build_macro_indicators(frame) is None


def test_all_null_columns_return_none() -> None:
    nulls = [float("nan")] * (MIN_OBSERVATIONS + 4)
    frame = _frame(
        intelligence_binance_funding_rate_pct=nulls,
        intelligence_stablecoin_reserve_ratio=nulls,
        intelligence_exchange_netflow_7d_zscore=nulls,
    )
    assert build_macro_indicators(frame) is None


def test_funding_zscore_is_positive_when_latest_print_is_high() -> None:
    funding = [0.01] * (MIN_OBSERVATIONS + 3) + [0.09]
    frame = _frame(intelligence_binance_funding_rate_pct=funding)
    indicators = build_macro_indicators(frame)
    assert indicators is not None
    assert indicators.funding_rate_zscore_avg > 1.0
    # Untrusted columns stay neutral rather than blocking the whole overlay.
    assert indicators.stablecoin_supply_growth_pct == 0.0
    assert indicators.net_exchange_inflow_zscore == 0.0


def test_flat_funding_window_is_not_trusted() -> None:
    """Zero dispersion means no z-score exists -- must not divide by zero."""
    frame = _frame(intelligence_binance_funding_rate_pct=[0.01] * (MIN_OBSERVATIONS + 4))
    assert build_macro_indicators(frame) is None


def test_stablecoin_growth_is_window_pct_change() -> None:
    ratio = [1.0] * (MIN_OBSERVATIONS + 3) + [1.5]
    frame = _frame(intelligence_stablecoin_reserve_ratio=ratio)
    indicators = build_macro_indicators(frame)
    assert indicators is not None
    assert indicators.stablecoin_supply_growth_pct == pytest.approx(50.0)


def test_stablecoin_growth_with_zero_base_is_not_trusted() -> None:
    ratio = [0.0] + [1.0] * (MIN_OBSERVATIONS + 3)
    frame = _frame(intelligence_stablecoin_reserve_ratio=ratio)
    assert build_macro_indicators(frame) is None


def test_netflow_zscore_passes_through_latest_value() -> None:
    netflow = [0.0] * (MIN_OBSERVATIONS + 3) + [2.5]
    frame = _frame(intelligence_exchange_netflow_7d_zscore=netflow)
    indicators = build_macro_indicators(frame)
    assert indicators is not None
    assert indicators.net_exchange_inflow_zscore == pytest.approx(2.5)


def test_non_finite_values_are_dropped_with_nulls() -> None:
    """A stored inf would otherwise poison mean/std for the whole window."""
    netflow = [np.inf] + [1.0] * (MIN_OBSERVATIONS + 3) + [3.0]
    frame = _frame(intelligence_exchange_netflow_7d_zscore=netflow)
    indicators = build_macro_indicators(frame)
    assert indicators is not None
    assert indicators.net_exchange_inflow_zscore == pytest.approx(3.0)


def test_inf_only_column_falls_back_to_none() -> None:
    frame = _frame(intelligence_exchange_netflow_7d_zscore=[np.inf] * (MIN_OBSERVATIONS + 4))
    assert build_macro_indicators(frame) is None


def test_all_three_columns_populate_all_three_indicators() -> None:
    n = MIN_OBSERVATIONS + 4
    frame = _frame(
        intelligence_binance_funding_rate_pct=[0.01 * i for i in range(n)],
        intelligence_stablecoin_reserve_ratio=[1.0 + 0.01 * i for i in range(n)],
        intelligence_exchange_netflow_7d_zscore=[0.1 * i for i in range(n)],
    )
    indicators = build_macro_indicators(frame)
    assert indicators is not None
    assert indicators.funding_rate_zscore_avg > 0.0
    assert indicators.stablecoin_supply_growth_pct > 0.0
    assert indicators.net_exchange_inflow_zscore > 0.0


def test_min_observations_is_configurable() -> None:
    frame = _frame(intelligence_exchange_netflow_7d_zscore=[1.0, 2.0, 4.0])
    assert build_macro_indicators(frame) is None
    relaxed = build_macro_indicators(frame, min_observations=3)
    assert relaxed is not None
    assert relaxed.net_exchange_inflow_zscore == pytest.approx(4.0)
