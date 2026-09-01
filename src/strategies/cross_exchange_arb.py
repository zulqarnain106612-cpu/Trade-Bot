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

# Minimum *net* edge (bps, after round-trip cost) worth harvesting.
_MIN_SPREAD_BPS: float = 15.0
_MAX_SPREAD_BPS_FOR_FULL_CONFIDENCE: float = 60.0

# Both legs cross the book, so the gross spread has to cover two taker fees
# plus the slippage of lifting each side. 12bps ~= 2 x 4.5bps taker + 3bps
# slippage, i.e. a Binance/OKX pair at the default (non-VIP) tier. Callers on
# a better fee tier should pass their own figure rather than rely on this.
_DEFAULT_ROUND_TRIP_COST_BPS: float = 12.0


@dataclass(frozen=True, slots=True)
class CrossExchangeContext:
    """Bar-equivalent context: same-symbol prices on two venues."""

    venue_a: str
    price_a: float
    venue_b: str
    price_b: float


def compute_basis_bps(price_a: float, price_b: float) -> float:
    """Basis spread in basis points: (price_a - price_b) / price_b * 10_000."""
    if price_a <= 0:
        raise ValueError(f"price_a must be positive, got {price_a}")
    if price_b <= 0:
        raise ValueError(f"price_b must be positive, got {price_b}")
    return (price_a - price_b) / price_b * 10_000.0


class CrossExchangeArbStrategy:
    """
    Registry-conformant strategy: fades basis spread between two venues.

    The entry threshold and the confidence ramp both apply to the edge left
    after the round-trip cost of crossing both books, not to the gross spread.

    direction is w.r.t. venue_a: positive spread (a richer than b) ->
    short a / long b (direction = -1). Negative spread -> long a (direction = 1).
    """

    strategy_id: str = "cross_exchange_arb_v1"

    def __init__(
        self,
        max_capital_fraction: float = 0.10,
        round_trip_cost_bps: float = _DEFAULT_ROUND_TRIP_COST_BPS,
    ) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        if round_trip_cost_bps < 0.0:
            raise ValueError(f"round_trip_cost_bps must be non-negative, got {round_trip_cost_bps}")
        self._max_capital_fraction = max_capital_fraction
        self._round_trip_cost_bps = round_trip_cost_bps

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, CrossExchangeContext):
            raise TypeError(
                f"CrossExchangeArbStrategy requires a CrossExchangeContext, got {type(bar)}"
            )

        spread_bps = compute_basis_bps(bar.price_a, bar.price_b)
        # Only the edge left after crossing both books is harvestable; sizing
        # off the gross spread signals trades that fee and slippage eat.
        net_edge_bps = abs(spread_bps) - self._round_trip_cost_bps

        if net_edge_bps < _MIN_SPREAD_BPS:
            return Signal(direction=0, confidence=0.0, regime_fit=0.9)

        confidence = min(
            1.0,
            (net_edge_bps - _MIN_SPREAD_BPS)
            / (_MAX_SPREAD_BPS_FOR_FULL_CONFIDENCE - _MIN_SPREAD_BPS),
        )
        direction = -1 if spread_bps > 0 else 1
        # Regime-agnostic by construction — arbitrage works in any market state.
        return Signal(direction=direction, confidence=confidence, regime_fit=0.9)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
