"""
Advanced position sizing — Carver (2019) and López de Prado (2018).

Implements sizing approaches beyond the base half-Kelly:

  1. Carver forecast-scaled sizing — Systematic Trading Ch.4
     Position size ∝ forecast strength x risk target / volatility
  2. Volatility targeting — Carver (2019) Ch.2
     Size to hit a fixed % daily vol target regardless of asset vol
  3. Correlation-aware sizing — López de Prado (2018) AFML Ch.16
     Reduce size when new position correlates with existing book
  4. Bet-sizing from model probability — AFML Ch.10
     f = 2p - 1 discrete approximation vs Kelly continuous

All functions are pure.  No I/O.

Authority:
  - Carver (2019) Systematic Trading, Chapters 2, 4, 11
  - López de Prado (2018) AFML Chapters 10, 16
  - Kelly (1956) Bell System Technical Journal 35(4): 917-926
  - Thorp (2006) The Kelly Criterion in Blackjack, Sports Betting and the
    Stock Market — fractional Kelly derivation
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Carver (2019) recommended daily vol target as % of capital
_DEFAULT_DAILY_VOL_TARGET_PCT: Final[float] = 0.25  # 0.25% daily

# Carver minimum/maximum forecast scalar (before normalisation)
_FORECAST_SCALAR_MIN: Final[float] = -20.0
_FORECAST_SCALAR_MAX: Final[float] = 20.0

# AFML Ch.16 — correlation threshold above which to reduce new position
_CORRELATION_REDUCE_THRESHOLD: Final[float] = 0.7

# Minimum meaningful position size (avoid dust trades)
_MIN_NOTIONAL_USD: Final[float] = 10.0


# ---------------------------------------------------------------------------
# 1. Carver forecast-scaled position size — Systematic Trading Ch.4
# ---------------------------------------------------------------------------


def carver_forecast_position(
    capital_usd: float,
    forecast: float,
    daily_vol_pct: float,
    price: float,
    daily_vol_target_pct: float = _DEFAULT_DAILY_VOL_TARGET_PCT,
    forecast_scalar: float = 10.0,
) -> float:
    """
    Carver (2019) Ch.4 forecast-scaled position sizing.

    Position = (capital x vol_target_pct) / (forecast_scalar x price x daily_vol_pct)
                 x forecast

    Where:
      - forecast         : raw signal in (-20, 20), e.g. from ewm_trend_signal
      - daily_vol_pct    : annualised vol / sqrt(252), expressed as decimal
      - forecast_scalar  : normalisation constant (Carver recommends 10 for EWMAC)

    Returns notional_usd to trade.  Always non-negative — direction
    is handled by the caller (long/short).

    Carver (2019) p.72: "The position size is the forecast divided by the
    instrument risk, scaled to hit your target volatility."
    """
    if daily_vol_pct < 1e-6 or price < 1e-9:
        return 0.0
    if capital_usd <= 0:
        return 0.0

    clipped_forecast = float(np.clip(forecast, _FORECAST_SCALAR_MIN, _FORECAST_SCALAR_MAX))
    vol_risk_usd = capital_usd * (daily_vol_target_pct / 100.0)
    instrument_risk_usd = price * daily_vol_pct

    if instrument_risk_usd < 1e-9:
        return 0.0

    raw_notional = (
        (vol_risk_usd / instrument_risk_usd) * (abs(clipped_forecast) / forecast_scalar) * price
    )
    return max(0.0, min(raw_notional, capital_usd * 0.25))  # cap at 25% (Kelly ceiling)


# ---------------------------------------------------------------------------
# 2. Volatility targeting — Carver (2019) Ch.2
# ---------------------------------------------------------------------------


def vol_target_quantity(
    capital_usd: float,
    price: float,
    daily_vol_pct: float,
    daily_vol_target_pct: float = _DEFAULT_DAILY_VOL_TARGET_PCT,
) -> float:
    """
    Carver (2019) Ch.2 volatility targeting.

    Quantity = (capital x vol_target_pct) / (price x daily_vol_pct)

    Returns units of the asset to trade so that the position contributes
    exactly `daily_vol_target_pct` of daily volatility to the portfolio.

    Carver: "The most important decision in systematic trading is not
    what to trade, but how much to trade." (p.38)
    """
    if daily_vol_pct < 1e-6 or price < 1e-9 or capital_usd <= 0:
        return 0.0
    vol_cash = capital_usd * (daily_vol_target_pct / 100.0)
    qty = vol_cash / (price * daily_vol_pct)
    return max(0.0, qty)


def estimate_daily_vol(close: np.ndarray | list[float], window: int = 20) -> float:
    """
    Estimate daily volatility as ewm std of log returns.

    Carver (2019) Ch.2: use exponentially-weighted std with span=25 for
    a responsive but stable volatility estimate.

    Returns fraction (e.g. 0.02 = 2% daily vol).
    """
    arr = np.asarray(close, dtype=np.float64)
    if len(arr) < 2:
        return 0.01  # fallback 1%
    log_ret = np.diff(np.log(arr + 1e-12))
    if len(log_ret) < 2:
        return 0.01
    weights = np.exp(-np.arange(len(log_ret))[::-1] / (window / 2))
    weights /= weights.sum()
    ewm_var = float(np.sum(weights * (log_ret - np.average(log_ret, weights=weights)) ** 2))
    return float(max(math.sqrt(ewm_var), 1e-6))


# ---------------------------------------------------------------------------
# 3. Correlation-aware size reduction — AFML Ch.16
# ---------------------------------------------------------------------------


def correlation_adjusted_notional(
    proposed_notional_usd: float,
    avg_correlation_with_book: float,
    threshold: float = _CORRELATION_REDUCE_THRESHOLD,
) -> float:
    """
    López de Prado (2018) AFML Ch.16 — reduce position size when
    the new trade is highly correlated with existing open positions.

    If avg_correlation_with_book > threshold:
      notional *= (1 - correlation) / (1 - threshold)

    This prevents portfolio concentration when multiple signals fire
    simultaneously on correlated assets or timeframes.

    AFML Ch.16 p.241: "Bet size should reflect not just the signal
    strength but also the portfolio's marginal contribution to risk."
    """
    if avg_correlation_with_book <= threshold:
        return proposed_notional_usd
    # Linear reduction from 1x at threshold to 0x at correlation=1
    reduction = (1.0 - avg_correlation_with_book) / (1.0 - threshold)
    reduction = float(np.clip(reduction, 0.0, 1.0))
    return proposed_notional_usd * reduction


# ---------------------------------------------------------------------------
# 4. AFML Ch.10 bet-sizing from model probability
# ---------------------------------------------------------------------------


def afml_bet_size(
    p_long: float,
    capital_usd: float,
    max_fraction: float = 0.25,
) -> float:
    """
    López de Prado (2018) AFML Ch.10 — bet size from model probability.

    Discrete approximation: f = 2p - 1
    where p = model's P(long).

    This gives:
      p=0.5  → f=0.0 (no bet — no edge)
      p=0.7  → f=0.4 (moderate bet)
      p=0.9  → f=0.8 (strong bet, capped at max_fraction)

    AFML p.153: "The size of the bet should be proportional to the
    edge, which is 2p - 1."

    Returns notional_usd to deploy.
    """
    edge = float(np.clip(2.0 * p_long - 1.0, -1.0, 1.0))
    if edge <= 0:
        return 0.0
    fraction = min(edge, max_fraction)
    return capital_usd * fraction


# ---------------------------------------------------------------------------
# 5. Thorp fractional Kelly with variance penalty — Thorp (2006)
# ---------------------------------------------------------------------------


def thorp_kelly_with_variance(
    win_prob: float,
    win_loss_ratio: float,
    capital_usd: float,
    price: float,
    kelly_multiplier: float = 0.5,
    kelly_ceiling: float = 0.25,
    variance_penalty: float = 0.0,
) -> float:
    """
    Thorp (2006) fractional Kelly with optional variance penalty.

    Thorp: "When the variance of the return is high, reduce the Kelly
    fraction to avoid the risk of ruin from a run of losses."

    variance_penalty : additional fractional reduction [0, 1).
                       Set to realized_vol_ratio / 10 for auto-scaling.

    Returns notional_usd.
    """
    if win_prob <= 0 or win_prob >= 1 or win_loss_ratio <= 0:
        return 0.0
    q = 1.0 - win_prob
    kelly_f = (win_prob * win_loss_ratio - q) / win_loss_ratio
    kelly_f = max(0.0, kelly_f)
    adjusted = kelly_f * kelly_multiplier * (1.0 - float(np.clip(variance_penalty, 0.0, 0.5)))
    capped = min(adjusted, kelly_ceiling)
    if price < 1e-9:
        return 0.0
    return capital_usd * capped


# ---------------------------------------------------------------------------
# Combined sizing recommendation
# ---------------------------------------------------------------------------


def recommend_position_notional(
    capital_usd: float,
    price: float,
    p_long: float,
    win_prob: float,
    win_loss_ratio: float,
    forecast: float,
    daily_vol_pct: float,
    avg_book_correlation: float = 0.0,
    kelly_multiplier: float = 0.5,
    kelly_ceiling: float = 0.25,
) -> dict[str, float]:
    """
    Run the three sizing methods, take the minimum (most conservative), then
    apply the AFML Ch.16 correlation haircut to that minimum.

    This implements the Carver (2019) principle of "whichever method gives
    the smaller position" — reduces risk of oversizing in any single framework
    — and keeps the concentration control binding regardless of which method
    won.

    Returns dict with each method's notional, the correlation-adjusted
    minimum, and the recommended notional.
    """
    thorp = thorp_kelly_with_variance(
        win_prob,
        win_loss_ratio,
        capital_usd,
        price,
        kelly_multiplier,
        kelly_ceiling,
        variance_penalty=daily_vol_pct * 2,
    )
    afml = afml_bet_size(p_long, capital_usd, kelly_ceiling)
    carver = carver_forecast_position(
        capital_usd,
        forecast,
        daily_vol_pct,
        price,
    )
    # The correlation haircut applies to whichever method actually wins the
    # min, not to the Thorp leg alone. Adjusting thorp and *then* taking the
    # min meant a book at 0.95 correlation got the full, unreduced size
    # whenever Carver or AFML was the binding constraint — the concentration
    # control silently did nothing in exactly the cases it did not choose.
    raw_min = min(thorp, afml, carver)
    corr_adjusted = correlation_adjusted_notional(raw_min, avg_book_correlation)

    notionals = {
        "thorp_kelly": round(thorp, 2),
        "afml_bet_size": round(afml, 2),
        "carver_forecast": round(carver, 2),
        "correlation_adjusted": round(corr_adjusted, 2),
    }

    # UI-007: the most conservative (minimum) of the four methods, floored
    # to _MIN_NOTIONAL_USD -- but ONLY when that minimum is itself positive.
    # thorp/afml/carver each return exactly 0.0 to mean "no edge, do not
    # trade" (see afml_bet_size, thorp_kelly_with_variance above); flooring
    # a unanimous 0.0 up to _MIN_NOTIONAL_USD silently overrode that veto
    # and forced a minimum-size trade even when every method agreed there
    # was no edge. The floor exists only to avoid recommending a real but
    # sub-exchange-minimum notional (e.g. $0.03), not to manufacture a
    # trade out of no edge.
    recommended = 0.0 if corr_adjusted <= 0.0 else max(_MIN_NOTIONAL_USD, corr_adjusted)
    notionals["recommended"] = round(recommended, 2)
    return notionals
