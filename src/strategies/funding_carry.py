"""
Funding-rate carry strategy — v2 Sub-task 2, strategy family 2 of 4.

Harvests perpetual-futures funding payments: when funding is persistently
positive and stretched (longs pay shorts), short the perp and hold; when
persistently negative and stretched, go long. This is a carry trade, not a
directional bet — it profits from the funding payment itself, so it is
expected to be close to uncorrelated with momentum/regime and
mean-reversion price strategies (v2 diversification requirement).

Funding rate + its rolling z-score are already computed by the existing
intelligence providers (src/intelligence/providers/binance_provider.py
`_fetch_funding_data`, and the OKX/Bybit equivalents) — this strategy
consumes those pre-computed fields rather than re-fetching, per the v2
plan's directive to reuse existing fetch/aggregation logic.

Authority:
  - Perpetual swap funding mechanics: BitMEX (2016) perpetual contract
    whitepaper — funding rate as the price-tracking mechanism
  - Carver (2019) Systematic Trading Ch.11 — carry as a distinct return
    stream from trend/momentum
"""

from __future__ import annotations

from dataclasses import dataclass

from src.strategies.registry import Signal


# Funding rate stretched enough to harvest — Binance perp funding is capped
# at ±0.75% per 8h period on most pairs; 0.01% (1bp) is a conservative
# "meaningfully positive/negative" threshold for majors.
_MIN_ABS_RATE_PCT: float = 0.01
# Z-score threshold: only harvest when funding is unusually stretched
# relative to its own recent history, not just nonzero.
_ENTRY_ZSCORE: float = 1.5


@dataclass(frozen=True, slots=True)
class FundingContext:
    """Bar-equivalent context: latest funding rate + rolling z-score."""

    funding_rate_pct: float
    funding_zscore: float


class FundingCarryStrategy:
    """
    Registry-conformant strategy: fades stretched funding rate.

    direction is w.r.t. the perp: positive stretched funding (longs paying
    shorts) -> short the perp to collect funding (direction = -1).
    Negative stretched funding -> long the perp (direction = 1).
    """

    strategy_id: str = "funding_carry_v1"

    def __init__(self, max_capital_fraction: float = 0.10) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, FundingContext):
            raise TypeError(f"FundingCarryStrategy requires a FundingContext, got {type(bar)}")

        rate = bar.funding_rate_pct
        z = bar.funding_zscore
        abs_z = abs(z)

        if abs(rate) < _MIN_ABS_RATE_PCT or abs_z < _ENTRY_ZSCORE:
            return Signal(direction=0, confidence=0.0, regime_fit=0.5)

        direction = -1 if rate > 0 else 1
        confidence = min(1.0, abs_z / (_ENTRY_ZSCORE * 2))
        # Carry is regime-agnostic by construction — moderate, constant fit.
        return Signal(direction=direction, confidence=confidence, regime_fit=0.6)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
