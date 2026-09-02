"""
Mean-reversion pairs strategy — v2 Sub-task 2, strategy family 1 of 4.

Cointegration-based statistical arbitrage between two correlated assets:
if the price spread between symbol A and symbol B is stationary
(cointegrated), deviations from the spread's equilibrium mean are faded.

All functions are pure (no I/O) and operate on already-fetched OHLCV
DataFrames — data acquisition stays in the existing providers
(src/intelligence/providers/*), consistent with the v2 plan's directive
not to duplicate fetch logic.

Authority:
  - Engle & Granger (1987) "Co-integration and Error Correction" —
    two-step cointegration test used for pair selection
  - López de Prado (2018) AFML Ch.16 — portfolio construction, pairs
  - Vidyamurthy (2004) Pairs Trading — spread z-score entry/exit rules
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint

from src.strategies.registry import Signal

# Entry/exit thresholds — Vidyamurthy (2004) Ch.3 standard z-score bands.
_ENTRY_Z: float = 2.0
_EXIT_Z: float = 0.5
_COINT_PVALUE_MAX: float = 0.05
_MIN_LOOKBACK_BARS: int = 60


@dataclass(frozen=True, slots=True)
class CointegrationResult:
    """Engle-Granger two-step test outcome for a candidate pair."""

    is_cointegrated: bool
    pvalue: float
    hedge_ratio: float


def check_cointegration(price_a: pd.Series, price_b: pd.Series) -> CointegrationResult:
    """
    Engle-Granger cointegration test between two price series.

    hedge_ratio is the OLS beta of price_a on price_b (price_a ~ beta * price_b),
    used to construct the stationary spread: spread = price_a - beta * price_b.
    """
    if len(price_a) < _MIN_LOOKBACK_BARS or len(price_b) < _MIN_LOOKBACK_BARS:
        raise ValueError(
            f"need >= {_MIN_LOOKBACK_BARS} bars for cointegration test, "
            f"got {len(price_a)} / {len(price_b)}"
        )
    if len(price_a) != len(price_b):
        raise ValueError("price_a and price_b must be the same length")

    _, pvalue, _ = coint(price_a.to_numpy(), price_b.to_numpy())

    cov = np.cov(price_a.to_numpy(), price_b.to_numpy())
    hedge_ratio = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] != 0 else 0.0

    return CointegrationResult(
        is_cointegrated=bool(pvalue < _COINT_PVALUE_MAX),
        pvalue=float(pvalue),
        hedge_ratio=hedge_ratio,
    )


def compute_spread_zscore(
    price_a: pd.Series, price_b: pd.Series, hedge_ratio: float, window: int = 30
) -> pd.Series:
    """Rolling z-score of the cointegration spread."""
    spread = price_a - hedge_ratio * price_b
    mean = spread.rolling(window=window, min_periods=window).mean()
    std = spread.rolling(window=window, min_periods=window).std(ddof=0)
    return (spread - mean) / std.replace(0.0, np.nan)


@dataclass(frozen=True, slots=True)
class PairContext:
    """Bar-equivalent context passed to generate_signal() for a pair."""

    price_a: pd.Series
    price_b: pd.Series
    hedge_ratio: float
    window: int = 30


class MeanReversionPairsStrategy:
    """
    Registry-conformant strategy: fades spread z-score extremes on a
    cointegrated pair. direction is w.r.t. symbol A (long A/short B when
    spread is far below mean, short A/long B when far above).
    """

    strategy_id: str = "mean_reversion_pairs_v1"

    def __init__(self, max_capital_fraction: float = 0.15) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, PairContext):
            raise TypeError(f"MeanReversionPairsStrategy requires a PairContext, got {type(bar)}")

        zscore_series = compute_spread_zscore(bar.price_a, bar.price_b, bar.hedge_ratio, bar.window)
        if zscore_series.empty or pd.isna(zscore_series.iloc[-1]):
            return Signal(direction=0, confidence=0.0, regime_fit=0.0)

        z = float(zscore_series.iloc[-1])
        abs_z = abs(z)

        if abs_z < _EXIT_Z:
            return Signal(direction=0, confidence=0.0, regime_fit=0.5)
        if abs_z < _ENTRY_Z:
            return Signal(direction=0, confidence=0.0, regime_fit=0.5)

        direction = -1 if z > 0 else 1
        confidence = min(1.0, (abs_z - _ENTRY_Z) / _ENTRY_Z + 0.5)
        return Signal(direction=direction, confidence=confidence, regime_fit=0.7)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
