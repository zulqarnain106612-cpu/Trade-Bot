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

import structlog

from src.config import StrategyPortfolioSettings, get_settings
from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps, compute_greeks
from src.strategies.registry import Signal

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


_MIN_IV_ZSCORE: float = 1.0


@dataclass(frozen=True, slots=True)
class OptionsCarryContext:
    """
    implied_vol_zscore: current IV vs its own rolling history — high
    positive z means options are rich (good time to sell premium).
    holding_direction: 1 if long the underlying (covered call candidate),
    -1 if flat/short and willing to go long via cash-secured put, 0 if
    neither position is appropriate right now.

    The remaining fields describe the contract that would be sold, and the
    book it would join, so the strategy can evaluate the v5 Greeks ceilings
    (src/risk/greeks.py). They default to None because the Greeks check is
    only active once an operator configures a cap; see
    OptionsCarryStrategy.generate_signal for what happens when a cap is set
    but these are absent.

    spot / strike / time_to_expiry_years / implied_vol / is_call describe the
    contract. contracts is the number sold (positive). portfolio_delta and
    portfolio_vega are the book's current Greeks, before this trade.
    """

    implied_vol_zscore: float
    holding_direction: int
    spot: float | None = None
    strike: float | None = None
    time_to_expiry_years: float | None = None
    implied_vol: float | None = None
    is_call: bool = True
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
        cfg: StrategyPortfolioSettings | None = None,
    ) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction
        # Resolved from config when not injected, so a caller that has no
        # settings object (a bare constructor in a test, say) still gets the
        # configured ceilings rather than silently none.
        self._greeks_caps = greeks_caps if greeks_caps is not None else _caps_from_config(cfg)

    def generate_signal(self, bar: object) -> Signal:
        if not isinstance(bar, OptionsCarryContext):
            raise TypeError(
                f"OptionsCarryStrategy requires an OptionsCarryContext, got {type(bar)}"
            )

        if bar.implied_vol_zscore < _MIN_IV_ZSCORE or bar.holding_direction == 0:
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        within, reason = self._greeks_within_caps(bar)
        if not within:
            log.warning("options_carry.greeks_cap_veto", reason=reason)
            return Signal(direction=0, confidence=0.0, regime_fit=0.6)

        confidence = min(1.0, (bar.implied_vol_zscore - _MIN_IV_ZSCORE) / _MIN_IV_ZSCORE + 0.5)
        direction = 1 if bar.holding_direction > 0 else -1
        return Signal(direction=direction, confidence=confidence, regime_fit=0.6)

    def _greeks_within_caps(self, bar: OptionsCarryContext) -> tuple[bool, str]:
        """
        Check the post-trade book against the configured Greeks ceilings.

        Notional-based Kelly sizing cannot see this: a short option is small
        in notional terms and still carries unbounded directional and vol
        exposure. That is the whole reason greeks.py exists separately.

        No caps configured -> no check, matching the behaviour before the
        setting existed. Caps configured but contract terms missing or
        unusable -> veto: once an operator has asked for a ceiling, waving
        through a position whose Greeks cannot be measured defeats it.
        """
        caps = self._greeks_caps
        if caps is None:
            return True, "no greeks caps configured"

        terms = (bar.spot, bar.strike, bar.time_to_expiry_years, bar.implied_vol)
        if any(t is None for t in terms):
            return False, "greeks caps configured but contract terms are missing"

        try:
            greeks = compute_greeks(
                spot=float(bar.spot),  # type: ignore[arg-type]
                strike=float(bar.strike),  # type: ignore[arg-type]
                time_to_expiry_years=float(bar.time_to_expiry_years),  # type: ignore[arg-type]
                volatility=float(bar.implied_vol),  # type: ignore[arg-type]
                is_call=bar.is_call,
            )
        except ValueError as exc:
            return False, f"greeks not computable: {exc}"

        # This strategy SELLS premium, so the position carries the negative
        # of the long-option Greeks, scaled by contract count.
        size = -abs(bar.contracts)
        return check_greeks_within_caps(
            portfolio_delta=bar.portfolio_delta + size * greeks.delta,
            portfolio_vega=bar.portfolio_vega + size * greeks.vega,
            caps=caps,
        )

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction


def _caps_from_config(cfg: StrategyPortfolioSettings | None) -> GreeksExposureCaps | None:
    """
    Build caps from settings, or None when the operator configured neither.

    Half-configuration cannot reach here: StrategyPortfolioSettings rejects
    one-sided caps at startup, because capping one Greek and leaving the
    other unbounded is not a meaningful exposure limit. Checking one field
    for None is therefore sufficient.
    """
    cfg = cfg if cfg is not None else get_settings().strategy_portfolio
    max_delta = cfg.options_carry_max_abs_delta
    max_vega = cfg.options_carry_max_abs_vega
    if max_delta is None or max_vega is None:
        return None
    return GreeksExposureCaps(max_abs_delta=float(max_delta), max_abs_vega=float(max_vega))
