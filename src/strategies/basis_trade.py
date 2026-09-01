"""
Spot-perp basis trade strategy — v5 Derivatives & Structured Strategies.

Distinct from v3's cross_exchange_arb (same instrument, two venues): this
trades the basis between spot and perpetual futures on the *same* venue,
which is driven by funding-rate expectations rather than venue-liquidity
divergence — a different, complementary source of carry.

Authority:
  - Hull (2018) Options, Futures, and Other Derivatives Ch.5 — cost-of-carry
    futures pricing model
"""

from __future__ import annotations

from dataclasses import dataclass

from src.strategies.registry import Signal

_MIN_ANNUALIZED_BASIS_PCT: float = 5.0
_MAX_ANNUALIZED_BASIS_PCT_FOR_FULL_CONFIDENCE: float = 30.0


@dataclass(frozen=True, slots=True)
class BasisTradeContext:
    spot_price: float
    perp_price: float
    # Days over which the spot-perp gap is assumed to converge. This is the
    # denominator of the annualization and therefore sets the entire scale of
    # the signal — see compute_annualized_basis_pct. It was previously
    # accepted here and ignored, with the horizon hardcoded to 1 day.
    days_to_perp_funding_normalization: float = 1.0


def compute_annualized_basis_pct(
    spot_price: float,
    perp_price: float,
    days_to_convergence: float = 1.0,
) -> float:
    """
    Annualized basis: (perp - spot) / spot * 100 * (365 / days_to_convergence).

    The horizon is the whole signal. A raw perp-spot gap is a few basis
    points; what makes it a carry number is the assumption about how often
    you capture it. At the default 1-day horizon the raw gap is multiplied by
    365, so a 0.0137% gap already clears the strategy's 5% entry threshold
    and a 0.082% gap saturates its 30% full-confidence ceiling — both routine
    for a perp. That is a deliberate carry-strength convention, not fair-value
    calendar-spread pricing, but it means the thresholds are only meaningful
    relative to the horizon they were calibrated against.

    The parameter exists so that horizon is stated rather than buried. It was
    previously hardcoded to 1 while BasisTradeContext carried a field for it
    that nothing read, and the docstring described a 365/365 = 1-year
    convergence — off by a factor of 365 from what the code actually did.
    The default preserves the existing behaviour exactly.
    """
    if spot_price <= 0:
        raise ValueError(f"spot_price must be positive, got {spot_price}")
    if perp_price <= 0:
        # A non-positive perp mark is bad data, not deep backwardation: it
        # would yield a <= -100% raw gap and a confidently-saturated long.
        raise ValueError(f"perp_price must be positive, got {perp_price}")
    if days_to_convergence <= 0:
        # A zero or negative horizon is not "instant convergence", it is a
        # division by zero wearing a plausible name.
        raise ValueError(f"days_to_convergence must be positive, got {days_to_convergence}")
    raw_pct = (perp_price - spot_price) / spot_price * 100.0
    return raw_pct * (365.0 / days_to_convergence)


class BasisTradeStrategy:
    """
    Registry-conformant strategy: fades a stretched spot-perp basis.

    Positive basis (perp > spot, contango) -> short perp / long spot
    (direction = -1, w.r.t. the perp). Negative basis (backwardation) ->
    long perp / short spot (direction = 1).
    """

    strategy_id: str = "basis_trade_v1"

    def __init__(self, max_capital_fraction: float = 0.10) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, BasisTradeContext):
            raise TypeError(f"BasisTradeStrategy requires a BasisTradeContext, got {type(bar)}")

        basis_pct = compute_annualized_basis_pct(
            bar.spot_price,
            bar.perp_price,
            bar.days_to_perp_funding_normalization,
        )
        abs_basis = abs(basis_pct)

        if abs_basis < _MIN_ANNUALIZED_BASIS_PCT:
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        confidence = min(
            1.0,
            (abs_basis - _MIN_ANNUALIZED_BASIS_PCT)
            / (_MAX_ANNUALIZED_BASIS_PCT_FOR_FULL_CONFIDENCE - _MIN_ANNUALIZED_BASIS_PCT),
        )
        direction = -1 if basis_pct > 0 else 1
        return Signal(direction=direction, confidence=confidence, regime_fit=0.6)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
