"""
Funding rate carry strategy — capture perpetual futures funding payments.

In crypto perpetual futures, the funding rate mechanism keeps the contract
price close to spot by charging longs and paying shorts (or vice versa).
When the funding rate is elevated (market is over-leveraged long), shorts
earn a carry premium on top of any directional move.

This module provides signal scoring and position sizing inputs for a
funding carry trade:

  • Score:  normalised [-1, +1] carry signal from the current funding rate.
            +1 = strong short-carry opportunity (high positive funding).
            -1 = reverse carry opportunity (deeply negative funding).
             0 = neutral / near-zero funding.

  • Gate:   ``is_tradeable()`` returns False when the annualised carry
            falls below the minimum threshold (e.g. < 10% APR net of fees).

  • Sizing: ``suggested_notional()`` scales position with carry APR.

This module is purely computational (no I/O). It consumes the
``binance_funding_rate_pct`` field that the IntelligenceProvider already
populates and produces a standardised CarrySignal suitable for the
signal engine or a standalone carry strategy executor.

Authority:
  Deribit (2019) Perpetual Swap Mechanics — funding rate formula.
  Chan (2013) Algorithmic Trading Ch.7 — futures carry and basis trades.
  Burghardt & Hoskins (1994) "The Convexity Bias in Eurodollar Futures" —
    carry decomposition in derivatives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Perpetual funding intervals per day (Binance settles every 8h → 3/day)
_FUNDINGS_PER_DAY: Final[int] = 3
_DAYS_PER_YEAR: Final[int] = 365

# Default thresholds
_DEFAULT_MIN_APR_PCT: Final[float] = 10.0  # minimum annualised carry (net of fees)
_DEFAULT_FEE_PER_TRADE_PCT: Final[float] = 0.05  # entry + exit taker fees (0.05%)
_DEFAULT_MAX_NOTIONAL_USD: Final[float] = 50_000.0
_DEFAULT_CARRY_SCALE: Final[float] = 5.0  # score saturates at ±5% 8h rate

_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Carry signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarrySignal:
    """Output of one funding carry evaluation."""

    funding_rate_pct: float  # raw 8h funding rate (%)
    annualised_apr_pct: float  # gross funding APR (%)
    net_apr_pct: float  # net of round-trip fees (%)
    carry_score: float  # normalised [-1, +1]
    is_tradeable: bool  # True when net_apr exceeds minimum threshold
    direction: int  # +1 = short carry, -1 = reverse carry, 0 = neutral
    reject_reason: str  # empty when tradeable

    def to_dict(self) -> dict:
        return {
            "funding_rate_pct": round(self.funding_rate_pct, 6),
            "annualised_apr_pct": round(self.annualised_apr_pct, 2),
            "net_apr_pct": round(self.net_apr_pct, 2),
            "carry_score": round(self.carry_score, 4),
            "is_tradeable": self.is_tradeable,
            "direction": self.direction,
            "reject_reason": self.reject_reason,
        }


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------


def evaluate_carry(
    funding_rate_pct: float,
    min_apr_pct: float = _DEFAULT_MIN_APR_PCT,
    fee_pct: float = _DEFAULT_FEE_PER_TRADE_PCT,
    carry_scale: float = _DEFAULT_CARRY_SCALE,
) -> CarrySignal:
    """
    Compute a carry signal from the current 8h funding rate.

    Parameters
    ----------
    funding_rate_pct:
        Current 8h funding rate as a percentage (e.g. 0.01 = 0.01% per 8h).
        Positive = longs pay shorts (short carry opportunity).
        Negative = shorts pay longs (reverse carry opportunity).
    min_apr_pct:
        Minimum net annualised return required to declare tradeable.
    fee_pct:
        Total round-trip fee (entry taker + exit taker, same side).
        Deducted from gross APR.
    carry_scale:
        Funding rate at which score saturates to ±1. Default = 0.05%.
        E.g. a 0.05% 8h rate → carry_score = +1.

    Returns
    -------
    CarrySignal with gross/net APR, normalised score, tradeability gate.
    """
    # Gross APR: compound over all 8h periods in a year
    # approx: rate * fundings_per_day * days_per_year (for small rates, exact = compounding)
    periods_per_year = _FUNDINGS_PER_DAY * _DAYS_PER_YEAR
    gross_apr = funding_rate_pct * periods_per_year  # simple approximation

    # Fee cost in APR terms: 2 round-trips per year assumed for fair comparison.
    # Net APR: preserve sign so negative funding stays negative.
    sign = 1.0 if funding_rate_pct >= 0 else -1.0
    fee_drag = 2.0 * fee_pct  # entry + exit
    net_apr = gross_apr - sign * fee_drag

    # Normalised score: tanh(rate / scale) gives smooth [-1, +1]
    # carry_scale in percent to match funding_rate_pct units
    carry_score = math.tanh(funding_rate_pct / (carry_scale + _EPS))

    # Tradeability gate: |net_apr| must exceed min_apr
    abs_net = abs(net_apr)
    if abs_net < min_apr_pct:
        return CarrySignal(
            funding_rate_pct=funding_rate_pct,
            annualised_apr_pct=gross_apr,
            net_apr_pct=net_apr,
            carry_score=carry_score,
            is_tradeable=False,
            direction=0,
            reject_reason=f"net_apr={net_apr:.2f}% < min={min_apr_pct:.1f}%",
        )

    direction = 1 if funding_rate_pct > 0 else -1
    log.debug(
        "funding_carry.signal",
        rate_pct=round(funding_rate_pct, 6),
        gross_apr=round(gross_apr, 2),
        net_apr=round(net_apr, 2),
        direction=direction,
    )

    return CarrySignal(
        funding_rate_pct=funding_rate_pct,
        annualised_apr_pct=gross_apr,
        net_apr_pct=net_apr,
        carry_score=carry_score,
        is_tradeable=True,
        direction=direction,
        reject_reason="",
    )


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def suggested_notional(
    signal: CarrySignal,
    capital_usd: float,
    risk_target_pct: float = 1.0,
    max_notional_usd: float = _DEFAULT_MAX_NOTIONAL_USD,
) -> float:
    """
    Suggest a notional position size for the carry trade.

    Scales with net APR and risk_target_pct, capped at max_notional_usd.

    Parameters
    ----------
    signal:
        CarrySignal from evaluate_carry().
    capital_usd:
        Current trading capital in USD.
    risk_target_pct:
        Fraction of capital to risk per unit of carry APR.
        Default 1.0 → at 10% APR, use 10% of capital.
    max_notional_usd:
        Hard cap on notional regardless of carry.

    Returns
    -------
    Notional in USD (0 if signal is not tradeable).
    """
    if not signal.is_tradeable or capital_usd <= 0:
        return 0.0

    # Scale notional by net APR normalised to the minimum threshold
    apr_ratio = abs(signal.net_apr_pct) / max(_DEFAULT_MIN_APR_PCT, _EPS)
    raw_notional = capital_usd * (risk_target_pct / 100.0) * apr_ratio

    return min(raw_notional, max_notional_usd)


# ---------------------------------------------------------------------------
# Regime filter
# ---------------------------------------------------------------------------


def is_carry_regime(
    funding_rate_pct: float,
    futures_oi_change_pct: float,
    liquidation_pressure_zscore: float,
    *,
    min_funding_pct: float = 0.005,
    max_oi_change_pct: float = 20.0,
    max_liquidation_zscore: float = 2.0,
) -> tuple[bool, str]:
    """
    Sanity-check whether the current market regime supports a carry trade.

    A carry trade is risky when:
      - OI is expanding rapidly (crowded, reversal risk).
      - Liquidation pressure is elevated (cascade risk).

    Returns (is_safe, reason). reason is empty when safe.
    """
    if abs(funding_rate_pct) < min_funding_pct:
        return False, f"funding_rate={funding_rate_pct:.6f}% too low (min={min_funding_pct}%)"

    if abs(futures_oi_change_pct) > max_oi_change_pct:
        return (
            False,
            f"oi_change={futures_oi_change_pct:.1f}% too high (max={max_oi_change_pct}%)",
        )

    if abs(liquidation_pressure_zscore) > max_liquidation_zscore:
        return (
            False,
            f"liquidation_zscore={liquidation_pressure_zscore:.2f} > max={max_liquidation_zscore}",
        )

    return True, ""
