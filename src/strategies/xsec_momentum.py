"""
Cross-sectional momentum strategy — v2 Sub-task 2, strategy family 4 of 4.

Ranks the traded universe by trailing N-day return and takes the top
decile long / bottom decile short, rebalanced each tick. This differs
from the existing time-series/regime-conditioned signal (which asks "is
this asset going up") by asking "is this asset going up *relative to the
rest of the universe*" — a distinct return driver expected to be lowly
correlated with single-asset trend/mean-reversion/carry strategies.

All functions are pure and operate on a already-fetched return matrix
(one column per symbol) — no I/O.

Authority:
  - Jegadeesh & Titman (1993) "Returns to Buying Winners and Selling
    Losers" — cross-sectional momentum anomaly
  - Asness, Moskowitz & Pedersen (2013) "Value and Momentum Everywhere" —
    cross-asset momentum construction
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.strategies.registry import Signal


_DECILE_FRACTION: float = 0.1
_MIN_UNIVERSE_SIZE: int = 10


@dataclass(frozen=True, slots=True)
class UniverseContext:
    """
    Bar-equivalent context: trailing returns for the whole traded universe
    plus the symbol this generate_signal() call is being asked to rank.

    trailing_returns : Series indexed by symbol, N-day trailing return
    target_symbol    : the symbol whose Signal is being requested
    """

    trailing_returns: pd.Series
    target_symbol: str


def rank_universe(trailing_returns: pd.Series) -> pd.Series:
    """Percentile rank (0=worst, 1=best) of each symbol's trailing return."""
    return trailing_returns.rank(pct=True)


class CrossSectionalMomentumStrategy:
    """
    Registry-conformant strategy: long top decile / short bottom decile of
    the universe by trailing return, flat otherwise.
    """

    strategy_id: str = "xsec_momentum_v1"

    def __init__(self, max_capital_fraction: float = 0.15) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, UniverseContext):
            raise TypeError(
                f"CrossSectionalMomentumStrategy requires a UniverseContext, got {type(bar)}"
            )

        returns = bar.trailing_returns.dropna()
        if len(returns) < _MIN_UNIVERSE_SIZE or bar.target_symbol not in returns.index:
            return Signal(direction=0, confidence=0.0, regime_fit=0.0)

        percentiles = rank_universe(returns)
        target_pctile = float(percentiles.loc[bar.target_symbol])

        if target_pctile >= 1.0 - _DECILE_FRACTION:
            confidence = min(
                1.0, (target_pctile - (1.0 - _DECILE_FRACTION)) / _DECILE_FRACTION + 0.5
            )
            return Signal(direction=1, confidence=confidence, regime_fit=0.7)

        if target_pctile <= _DECILE_FRACTION:
            confidence = min(1.0, (_DECILE_FRACTION - target_pctile) / _DECILE_FRACTION + 0.5)
            return Signal(direction=-1, confidence=confidence, regime_fit=0.7)

        return Signal(direction=0, confidence=0.0, regime_fit=0.5)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
