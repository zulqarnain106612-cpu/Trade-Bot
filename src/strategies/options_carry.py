"""
Options carry strategy — v5 Derivatives & Structured Strategies.

Covered-call / cash-secured-put signal generation: sells premium against
existing directional exposure when implied volatility is rich relative to
its own recent history, harvesting the vol risk premium. Non-directional
relative to the v2 momentum/mean-reversion families — a distinct return
driver keyed on volatility level, not price direction.

A rich-IV signal is only actionable if the resulting short-premium position
keeps the book inside its Greeks ceilings, so the strategy optionally
consults src/risk/greeks.py before emitting a non-flat signal: notional
Kelly sizing downstream cannot see the non-linear delta/vega a short option
adds.

Authority:
  - Hull (2018) Options, Futures, and Other Derivatives Ch.19-20 — covered
    calls, cash-secured puts, implied vol
  - Israelov & Nielsen (2014) "Covered Calls Uncovered" — vol risk premium
    harvesting mechanics
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps, compute_greeks
from src.strategies.registry import Signal


_MIN_IV_ZSCORE: float = 1.0


@dataclass(frozen=True, slots=True)
class OptionsCarryContext:
    """
    implied_vol_zscore: current IV vs its own rolling history — high
    positive z means options are rich (good time to sell premium).
    holding_direction: 1 if long the underlying (covered call candidate),
    -1 if flat/short and willing to go long via cash-secured put, 0 if
    neither position is appropriate right now.

    The remaining fields describe the contract being considered and the
    book's current Greeks exposure. They are only consulted when the
    strategy was built with `greeks_caps`; a caps-less strategy behaves
    exactly as before and ignores them.
    """

    implied_vol_zscore: float
    holding_direction: int
    spot: float = 0.0
    strike: float = 0.0
    time_to_expiry_years: float = 0.0
    implied_vol: float = 0.0
    rate: float = 0.0
    contracts: float = 1.0
    portfolio_delta: float = 0.0
    portfolio_vega: float = 0.0


class OptionsCarryStrategy:
    """
    Registry-conformant strategy: sells premium (covered call or
    cash-secured put) when implied vol is stretched rich.

    direction here encodes the *premium-selling* action's underlying bias:
    1 = sell covered calls (already long, capping upside for premium),
    -1 = sell cash-secured puts (willing to acquire long exposure at a
    discount), 0 = flat (vol not rich enough to harvest).
    """

    strategy_id: str = "options_carry_v1"

    def __init__(
        self,
        max_capital_fraction: float = 0.10,
        greeks_caps: GreeksExposureCaps | None = None,
    ) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction
        self._greeks_caps = greeks_caps

    def _breaches_greeks_caps(self, bar: OptionsCarryContext, is_call: bool) -> bool:
        """
        Would selling this contract push the book past its Greeks ceilings?

        Fails closed: if caps are configured but the contract terms are
        missing or non-positive, the exposure cannot be measured, so the
        signal is suppressed rather than sized off an unmeasured position.
        """
        caps = self._greeks_caps
        if caps is None:
            return False

        try:
            greeks = compute_greeks(
                spot=bar.spot,
                strike=bar.strike,
                time_to_expiry_years=bar.time_to_expiry_years,
                volatility=bar.implied_vol,
                rate=bar.rate,
                is_call=is_call,
            )
        except ValueError:
            return True

        # Both branches sell premium, so the book takes the short side of the
        # contract's Greeks: short call adds negative delta, short put positive.
        sign = -bar.contracts
        within, _ = check_greeks_within_caps(
            portfolio_delta=bar.portfolio_delta + sign * greeks.delta,
            portfolio_vega=bar.portfolio_vega + sign * greeks.vega,
            caps=caps,
        )
        return not within

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, OptionsCarryContext):
            raise TypeError(
                f"OptionsCarryStrategy requires an OptionsCarryContext, got {type(bar)}"
            )

        if bar.implied_vol_zscore < _MIN_IV_ZSCORE or bar.holding_direction == 0:
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        direction = 1 if bar.holding_direction > 0 else -1
        if self._breaches_greeks_caps(bar, is_call=direction > 0):
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        confidence = min(1.0, (bar.implied_vol_zscore - _MIN_IV_ZSCORE) / _MIN_IV_ZSCORE + 0.5)
        return Signal(direction=direction, confidence=confidence, regime_fit=0.6)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
