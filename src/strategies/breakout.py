"""
Volume-weighted breakout strategy — v2 Sub-task 2, strategy family 3 of 4.

Trades range breakouts confirmed by volume: price must clear the recent
N-bar high/low AND do so on above-average volume, with an ATR-based
initial stop. Volume confirmation filters the common false-breakout case
where price pokes through a level on thin volume and reverts.

All functions are pure and operate on already-fetched OHLCV DataFrames.

Authority:
  - Wilder (1978) New Concepts in Technical Trading Systems — ATR
  - Darvas (1960) How I Made $2,000,000 in the Stock Market — box breakout
  - Granville (1963) OBV — volume confirms price direction
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.strategies.registry import Signal


_LOOKBACK_BARS: int = 20
_VOLUME_MULTIPLE: float = 1.5
_ATR_PERIOD: int = 14
_MIN_BARS_REQUIRED: int = max(_LOOKBACK_BARS, _ATR_PERIOD) + 1


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder (1978) Average True Range."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


@dataclass(frozen=True, slots=True)
class BreakoutContext:
    """Bar-equivalent context: recent OHLCV history ending at the current bar."""

    high: pd.Series
    low: pd.Series
    close: pd.Series
    volume: pd.Series


class BreakoutStrategy:
    """
    Registry-conformant strategy: N-bar range breakout confirmed by volume.
    """

    strategy_id: str = "breakout_volume_v1"

    def __init__(self, max_capital_fraction: float = 0.15) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, BreakoutContext):
            raise TypeError(f"BreakoutStrategy requires a BreakoutContext, got {type(bar)}")

        n = len(bar.close)
        if n < _MIN_BARS_REQUIRED:
            return Signal(direction=0, confidence=0.0, regime_fit=0.0)

        prior_high = bar.high.iloc[-(_LOOKBACK_BARS + 1) : -1].max()
        prior_low = bar.low.iloc[-(_LOOKBACK_BARS + 1) : -1].min()
        avg_volume = bar.volume.iloc[-(_LOOKBACK_BARS + 1) : -1].mean()

        last_close = float(bar.close.iloc[-1])
        last_volume = float(bar.volume.iloc[-1])

        atr = compute_atr(bar.high, bar.low, bar.close, _ATR_PERIOD)
        last_atr = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0

        volume_confirmed = avg_volume > 0 and last_volume >= _VOLUME_MULTIPLE * avg_volume

        if not volume_confirmed or last_atr <= 0:
            return Signal(direction=0, confidence=0.0, regime_fit=0.4)

        if last_close > prior_high:
            excess = (last_close - prior_high) / last_atr
            confidence = min(1.0, 0.5 + excess)
            return Signal(direction=1, confidence=confidence, regime_fit=0.8)

        if last_close < prior_low:
            excess = (prior_low - last_close) / last_atr
            confidence = min(1.0, 0.5 + excess)
            return Signal(direction=-1, confidence=confidence, regime_fit=0.8)

        return Signal(direction=0, confidence=0.0, regime_fit=0.4)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction


__all__ = ["BreakoutContext", "BreakoutStrategy", "compute_atr"]
