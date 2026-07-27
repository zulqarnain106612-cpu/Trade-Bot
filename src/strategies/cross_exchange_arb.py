"""
Cross-exchange basis arbitrage strategy — v3 Multi-Exchange Execution.

Detects when the same symbol's price diverges meaningfully across two
venues (basis spread) and signals a fade: long the cheaper venue / short
the richer venue. This is a distinct, close-to-market-neutral return
stream on top of v2's single-venue strategy families.

Authority:
  - Almgren & Chriss (2001) — basis spread as an execution/arbitrage signal
  - Chan (2013) Algorithmic Trading Ch.5 — cross-venue arbitrage mechanics
"""

from __future__ import annotations

from dataclasses import dataclass

from src.strategies.registry import Signal


# Minimum basis spread (bps) to consider worth harvesting after fees/slippage.
_MIN_SPREAD_BPS: float = 15.0
_MAX_SPREAD_BPS_FOR_FULL_CONFIDENCE: float = 60.0


@dataclass(frozen=True, slots=True)
class CrossExchangeContext:
    """Bar-equivalent context: same-symbol prices on two venues."""

    venue_a: str
    price_a: float
    venue_b: str
    price_b: float


def compute_basis_bps(price_a: float, price_b: float) -> float:
    """Basis spread in basis points: (price_a - price_b) / price_b * 10_000."""
    if price_b <= 0:
        raise ValueError(f"price_b must be positive, got {price_b}")
    return (price_a - price_b) / price_b * 10_000.0


class CrossExchangeArbStrategy:
    """
    Registry-conformant strategy: fades basis spread between two venues.

    direction is w.r.t. venue_a: positive spread (a richer than b) ->
    short a / long b (direction = -1). Negative spread -> long a (direction = 1).
    """

    strategy_id: str = "cross_exchange_arb_v1"

    def __init__(self, max_capital_fraction: float = 0.10) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, CrossExchangeContext):
            raise TypeError(
                f"CrossExchangeArbStrategy requires a CrossExchangeContext, got {type(bar)}"
            )

        spread_bps = compute_basis_bps(bar.price_a, bar.price_b)
        abs_spread = abs(spread_bps)

        if abs_spread < _MIN_SPREAD_BPS:
            return Signal(direction=0, confidence=0.0, regime_fit=0.9)

        confidence = min(
            1.0,
            (abs_spread - _MIN_SPREAD_BPS)
            / (_MAX_SPREAD_BPS_FOR_FULL_CONFIDENCE - _MIN_SPREAD_BPS),
        )
        direction = -1 if spread_bps > 0 else 1
        # Regime-agnostic by construction — arbitrage works in any market state.
        return Signal(direction=direction, confidence=confidence, regime_fit=0.9)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
