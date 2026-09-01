"""
Options Greeks — v5 Derivatives & Structured Strategies.

Black-Scholes Greeks (delta, gamma, vega, theta) for European options, used
to size and cap options positions independently of notional-based Kelly
sizing. Extends the risk engine so it can reason about non-linear payoffs,
not just directional notional exposure.

Authority:
  - Black & Scholes (1973) "The Pricing of Options and Corporate
    Liabilities"
  - Hull (2018) Options, Futures, and Other Derivatives Ch.19 — Greeks
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT_2PI: float = math.sqrt(2 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    delta: float
    gamma: float
    vega: float
    theta: float


def _validate_inputs(
    spot: float, strike: float, time_to_expiry_years: float, volatility: float, rate: float
) -> None:
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    if time_to_expiry_years <= 0:
        raise ValueError(f"time_to_expiry_years must be positive, got {time_to_expiry_years}")
    if volatility <= 0:
        raise ValueError(f"volatility must be positive, got {volatility}")
    _ = rate  # rate may legitimately be 0 or negative; no validation needed


def compute_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    rate: float = 0.0,
    is_call: bool = True,
) -> OptionGreeks:
    """Black-Scholes Greeks for a European call or put."""
    _validate_inputs(spot, strike, time_to_expiry_years, volatility, rate)

    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * time_to_expiry_years) / (
        volatility * sqrt_t
    )
    d2 = d1 - volatility * sqrt_t

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1 vol-point (1%) move

    if is_call:
        delta = _norm_cdf(d1)
        theta = (
            -spot * pdf_d1 * volatility / (2 * sqrt_t)
            - rate * strike * math.exp(-rate * time_to_expiry_years) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -spot * pdf_d1 * volatility / (2 * sqrt_t)
            + rate * strike * math.exp(-rate * time_to_expiry_years) * _norm_cdf(-d2)
        ) / 365.0

    return OptionGreeks(delta=delta, gamma=gamma, vega=vega, theta=theta)


@dataclass(frozen=True, slots=True)
class GreeksExposureCaps:
    """Portfolio-level Greeks exposure ceilings, independent of Kelly notional."""

    max_abs_delta: float
    max_abs_vega: float


def check_greeks_within_caps(
    portfolio_delta: float, portfolio_vega: float, caps: GreeksExposureCaps
) -> tuple[bool, str]:
    """Returns (within_caps, reason). Never mutates portfolio state."""
    if abs(portfolio_delta) > caps.max_abs_delta:
        return False, (
            f"portfolio delta {portfolio_delta:.4f} exceeds cap {caps.max_abs_delta:.4f}"
        )
    if abs(portfolio_vega) > caps.max_abs_vega:
        return False, f"portfolio vega {portfolio_vega:.4f} exceeds cap {caps.max_abs_vega:.4f}"
    return True, "within caps"
