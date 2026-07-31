"""
Mean reversion strategy signals — statistical arbitrage on price z-scores.

Generates entry/exit signals based on price deviation from a rolling mean,
calibrated by the Ornstein-Uhlenbeck (OU) half-life of mean reversion.

Two signal generators:

  1. Bollinger Band Z-score:
     z = (price - rolling_mean) / rolling_std
     Entry at |z| > entry_threshold; exit when |z| < exit_threshold.
     (Bollinger 1992; Chan 2013 Algorithmic Trading Ch.3)

  2. Ornstein-Uhlenbeck half-life:
     Fits OU process to price series: dp = theta*(mu - p)*dt + sigma*dW
     Half-life = ln(2) / theta
     Only trade when half_life_bars falls in a reasonable range
     (too short = noise; too long = structural trend, not reversion).

All functions are pure, accept numpy/pandas arrays, and return structured
MeanReversionSignal dataclasses.

Authority:
  Chan (2013) Algorithmic Trading Ch.3 — mean reversion strategies.
  Bollinger (1992) Bollinger on Bollinger Bands — band-based signals.
  Uhlenbeck & Ornstein (1930) — OU process for mean-reverting series.
  Avellaneda & Lee (2010) "Statistical Arbitrage in the U.S. Equities
    Market" — half-life-based position sizing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Default parameters
_DEFAULT_LOOKBACK: Final[int] = 20  # rolling window (bars)
_DEFAULT_ENTRY_Z: Final[float] = 2.0  # |z| > 2 → entry
_DEFAULT_EXIT_Z: Final[float] = 0.5  # |z| < 0.5 → exit
_DEFAULT_MIN_HL_BARS: Final[int] = 2  # OU half-life must be >= 2 bars
_DEFAULT_MAX_HL_BARS: Final[int] = 120  # OU half-life must be <= 120 bars

_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeanReversionSignal:
    """
    Output of a mean reversion signal evaluation.

    direction:  +1 = long (price below mean, expect reversion up)
               -1 = short (price above mean, expect reversion down)
                0 = no signal (z-score within thresholds or model rejected)
    """

    z_score: float  # current deviation from mean
    rolling_mean: float
    rolling_std: float
    direction: int  # -1, 0, or +1
    is_entry: bool  # True when |z| crosses entry threshold
    is_exit: bool  # True when |z| falls below exit threshold
    confidence: float  # [0, 1] = |z| / entry_threshold, capped at 1
    reject_reason: str  # non-empty when direction == 0 due to filter


@dataclass(frozen=True)
class OUParams:
    """Estimated Ornstein-Uhlenbeck parameters."""

    theta: float  # mean-reversion speed
    mu: float  # long-run mean
    sigma: float  # volatility
    half_life_bars: float  # ln(2) / theta
    r_squared: float  # goodness of fit of the AR(1) regression


# ---------------------------------------------------------------------------
# Bollinger Band z-score signal
# ---------------------------------------------------------------------------


def bollinger_signal(
    prices: np.ndarray,
    lookback: int = _DEFAULT_LOOKBACK,
    entry_z: float = _DEFAULT_ENTRY_Z,
    exit_z: float = _DEFAULT_EXIT_Z,
) -> MeanReversionSignal:
    """
    Compute a mean reversion signal from Bollinger Band deviation.

    Parameters
    ----------
    prices:
        1D array of close prices (or log prices), chronological order.
        Must have at least ``lookback`` elements.
    lookback:
        Rolling window size for mean and std.
    entry_z:
        |z-score| threshold for a new entry signal.
    exit_z:
        |z-score| threshold for an exit signal (must be < entry_z).

    Returns
    -------
    MeanReversionSignal
    """
    n = len(prices)
    if n < lookback:
        return _no_signal(0.0, 0.0, 0.0, f"insufficient_bars={n} < lookback={lookback}")

    window = prices[-lookback:]
    mu = float(np.mean(window))
    std = float(np.std(window, ddof=1)) if lookback > 1 else 0.0

    if std < _EPS:
        return _no_signal(0.0, mu, std, "std_too_small")

    latest = float(prices[-1])
    z = (latest - mu) / std
    abs_z = abs(z)

    confidence = min(abs_z / max(entry_z, _EPS), 1.0)
    is_entry = abs_z >= entry_z
    is_exit = abs_z <= exit_z

    direction = 0
    if is_entry:
        direction = -1 if z > 0 else 1  # price above mean → short; below → long

    return MeanReversionSignal(
        z_score=z,
        rolling_mean=mu,
        rolling_std=std,
        direction=direction,
        is_entry=is_entry,
        is_exit=is_exit,
        confidence=confidence,
        reject_reason="" if is_entry or is_exit else f"z={z:.3f} between thresholds",
    )


# ---------------------------------------------------------------------------
# Ornstein-Uhlenbeck estimation
# ---------------------------------------------------------------------------


def estimate_ou_params(prices: np.ndarray) -> OUParams | None:
    """
    Estimate OU parameters via OLS on the AR(1) form:

        dp_t = a + b * p_{t-1} + eps_t

    theta = -ln(1 + b) / dt  (dt = 1 bar)
    mu    = -a / b
    half_life = ln(2) / theta

    Returns None if the series does not exhibit mean reversion (b >= 0).
    """
    if len(prices) < 4:
        return None

    p = np.asarray(prices, dtype=float)
    dp = np.diff(p)
    p_lag = p[:-1]

    # OLS: dp = a + b * p_lag
    try:
        A = np.column_stack([np.ones(len(p_lag)), p_lag])
        result = np.linalg.lstsq(A, dp, rcond=None)
        coeffs = result[0]
        residuals = result[1]
    except np.linalg.LinAlgError:
        return None

    a, b = float(coeffs[0]), float(coeffs[1])

    # b must be negative for mean reversion
    if b >= 0 or b <= -2:
        return None

    theta = -math.log(1.0 + b)
    if theta <= _EPS:
        return None

    mu_hat = -a / (b + _EPS)
    half_life = math.log(2.0) / theta

    # R^2
    ss_res = (
        float(residuals[0]) if len(residuals) > 0 else float(np.sum((dp - (a + b * p_lag)) ** 2))
    )
    ss_tot = float(np.var(dp, ddof=1)) * len(dp)
    r_sq = 1.0 - ss_res / max(ss_tot, _EPS)

    sigma_hat = math.sqrt(max(ss_res / max(len(dp) - 2, 1), 0.0))

    return OUParams(
        theta=theta,
        mu=mu_hat,
        sigma=sigma_hat,
        half_life_bars=half_life,
        r_squared=max(0.0, min(1.0, r_sq)),
    )


def is_mean_reverting(
    prices: np.ndarray,
    min_half_life: int = _DEFAULT_MIN_HL_BARS,
    max_half_life: int = _DEFAULT_MAX_HL_BARS,
) -> tuple[bool, OUParams | None, str]:
    """
    Test whether a price series is statistically mean-reverting.

    Returns (is_mr, ou_params, reason). reason is empty when is_mr=True.

    A series passes when:
      1. b < 0 in the AR(1) fit (necessary condition).
      2. Half-life is in [min_half_life, max_half_life] bars.
    """
    ou = estimate_ou_params(prices)

    if ou is None:
        return False, None, "no_mean_reversion_detected"

    if ou.half_life_bars < min_half_life:
        return (
            False,
            ou,
            f"half_life={ou.half_life_bars:.1f} < min={min_half_life}",
        )

    if ou.half_life_bars > max_half_life:
        return (
            False,
            ou,
            f"half_life={ou.half_life_bars:.1f} > max={max_half_life}",
        )

    return True, ou, ""


# ---------------------------------------------------------------------------
# Combined signal: Bollinger + OU gate
# ---------------------------------------------------------------------------


def mean_reversion_signal(
    prices: np.ndarray,
    lookback: int = _DEFAULT_LOOKBACK,
    entry_z: float = _DEFAULT_ENTRY_Z,
    exit_z: float = _DEFAULT_EXIT_Z,
    min_half_life: int = _DEFAULT_MIN_HL_BARS,
    max_half_life: int = _DEFAULT_MAX_HL_BARS,
    require_ou: bool = True,
) -> MeanReversionSignal:
    """
    Full mean reversion signal combining Bollinger z-score with OU gate.

    If ``require_ou=True`` (default), the signal is only tradeable when the
    series passes the OU mean-reversion test. This prevents taking reversal
    trades in a trending market.
    """
    bb = bollinger_signal(prices, lookback=lookback, entry_z=entry_z, exit_z=exit_z)

    if not require_ou:
        return bb

    if not bb.is_entry:
        return bb  # no entry signal anyway; skip OU test

    ok, ou, reason = is_mean_reverting(
        prices, min_half_life=min_half_life, max_half_life=max_half_life
    )
    if not ok:
        return _no_signal(
            bb.z_score, bb.rolling_mean, bb.rolling_std, f"ou_gate_rejected: {reason}"
        )

    return bb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_signal(z: float, mu: float, std: float, reason: str) -> MeanReversionSignal:
    return MeanReversionSignal(
        z_score=z,
        rolling_mean=mu,
        rolling_std=std,
        direction=0,
        is_entry=False,
        is_exit=False,
        confidence=0.0,
        reject_reason=reason,
    )
