"""
Options carry strategy — v5 Derivatives & Structured Strategies.

Covered-call / cash-secured-put signal generation: sells premium against
existing directional exposure when implied volatility is rich relative to
its own recent history, harvesting the vol risk premium. Non-directional
relative to the v2 momentum/mean-reversion families — a distinct return
driver keyed on volatility level, not price direction.

Authority:
  - Hull (2018) Options, Futures, and Other Derivatives Ch.19-20 — covered
    calls, cash-secured puts, implied vol
  - Israelov & Nielsen (2014) "Covered Calls Uncovered" — vol risk premium
    harvesting mechanics
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """

    implied_vol_zscore: float
    holding_direction: int


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

    def __init__(self, max_capital_fraction: float = 0.10) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, OptionsCarryContext):
            raise TypeError(
                f"OptionsCarryStrategy requires an OptionsCarryContext, got {type(bar)}"
            )

        if bar.implied_vol_zscore < _MIN_IV_ZSCORE or bar.holding_direction == 0:
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        confidence = min(1.0, (bar.implied_vol_zscore - _MIN_IV_ZSCORE) / _MIN_IV_ZSCORE + 0.5)
        direction = 1 if bar.holding_direction > 0 else -1
        return Signal(direction=direction, confidence=confidence, regime_fit=0.6)

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
