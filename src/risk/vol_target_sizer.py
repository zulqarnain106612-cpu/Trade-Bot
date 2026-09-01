"""
Volatility-targeted position sizing.

Sizes positions so that each trade contributes a fixed daily-vol equivalent
to the portfolio, scaled down during drawdowns (drawdown haircut).

Blends two methods:
  1. Vol-target  — notional = (target_vol_pct / realized_vol_pct) * capital
  2. Kelly blend — multiplies the vol-target notional by a half-Kelly scalar
     based on win-rate / payoff-ratio, further capped at a hard maximum.

Drawdown haircut: when equity drawdown from HWM exceeds ``dd_warn_pct``,
the final notional is reduced proportionally; at ``dd_halt_pct`` sizing
returns 0 (no new positions).

All functions are pure with no I/O.

Authority:
  Hurst, Ooi & Pedersen (2012) "A Century of Evidence on Trend-Following
    Strategies" AQR Capital — annualised vol-target sizing.
  Lopez de Prado (2018) AFML Ch.10 — Kelly fraction with drawdown overlay.
  Carver (2019) "Systematic Trading" Ch.9 — volatility scaling with
    instrument-level targets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Default parameters
_DEFAULT_TARGET_VOL_PCT: Final[float] = 1.0  # 1% daily vol target per position
_DEFAULT_KELLY_FRACTION: Final[float] = 0.5  # half-Kelly multiplier
_DEFAULT_MAX_NOTIONAL_PCT: Final[float] = 0.25  # 25% of capital hard cap
_DEFAULT_DD_WARN_PCT: Final[float] = 0.10  # start tapering at 10% drawdown
_DEFAULT_DD_HALT_PCT: Final[float] = 0.20  # stop sizing at 20% drawdown
_MIN_VOL: Final[float] = 1e-4  # floor to avoid division by near-zero vol
_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SizeResult:
    """
    Result of a volatility-targeted position size calculation.

    All monetary values are in USD.
    """

    notional_usd: float  # recommended position size
    vol_target_notional: float  # raw vol-target before Kelly and caps
    kelly_scalar: float  # Kelly fraction applied (0-1)
    dd_haircut: float  # drawdown haircut applied (0-1; 1 = no cut)
    realized_vol_pct: float  # annualised vol used in sizing
    reject_reason: str  # non-empty when notional_usd == 0

    def to_dict(self) -> dict:
        return {
            "notional_usd": round(self.notional_usd, 2),
            "vol_target_notional": round(self.vol_target_notional, 2),
            "kelly_scalar": round(self.kelly_scalar, 4),
            "dd_haircut": round(self.dd_haircut, 4),
            "realized_vol_pct": round(self.realized_vol_pct, 4),
            "reject_reason": self.reject_reason,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _annualised_vol(returns: list[float], periods_per_year: int = 252) -> float:
    """
    Compute annualised daily-return volatility.

    Parameters
    ----------
    returns:
        List of daily P&L returns (as fractions, e.g. 0.01 = 1%).
    periods_per_year:
        Trading periods per year. Default 252 (daily crypto ~ 365 works too).
    """
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(variance * periods_per_year) * 100.0  # as pct


def _kelly_scalar(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Half-Kelly scalar in [0, 1] from win-rate and payoff ratio.

    f* = (p*b - q) / b  where b = avg_win/avg_loss, q = 1 - p
    Returns half-Kelly = f* / 2, clamped to [0, 1].
    """
    if avg_loss <= _EPS or win_rate <= 0.0:
        return 0.0
    b = avg_win / avg_loss
    q = 1.0 - win_rate
    f_star = (win_rate * b - q) / b
    half_kelly = max(0.0, f_star / 2.0)
    return min(half_kelly, 1.0)


def _drawdown_haircut(
    current_equity: float,
    hwm: float,
    dd_warn_pct: float,
    dd_halt_pct: float,
) -> float:
    """
    Returns a multiplier [0, 1] to apply to position size.

    - No drawdown (equity >= HWM): 1.0
    - Drawdown in (dd_warn_pct, dd_halt_pct): linear taper from 1 to 0
    - Drawdown >= dd_halt_pct: 0.0
    """
    if hwm <= _EPS:
        return 1.0
    dd = (hwm - current_equity) / hwm
    if dd <= 0:
        return 1.0
    if dd >= dd_halt_pct:
        return 0.0
    if dd <= dd_warn_pct:
        return 1.0
    # Linear taper from warn to halt
    taper_range = dd_halt_pct - dd_warn_pct
    return 1.0 - (dd - dd_warn_pct) / taper_range


# ---------------------------------------------------------------------------
# Main sizing function
# ---------------------------------------------------------------------------


def vol_target_size(
    capital_usd: float,
    current_equity: float,
    hwm: float,
    realized_vol_pct: float,
    *,
    target_vol_pct: float = _DEFAULT_TARGET_VOL_PCT,
    win_rate: float = 0.50,
    avg_win_usd: float = 1.0,
    avg_loss_usd: float = 1.0,
    kelly_fraction: float = _DEFAULT_KELLY_FRACTION,
    max_notional_pct: float = _DEFAULT_MAX_NOTIONAL_PCT,
    dd_warn_pct: float = _DEFAULT_DD_WARN_PCT,
    dd_halt_pct: float = _DEFAULT_DD_HALT_PCT,
    periods_per_year: int = 252,
) -> SizeResult:
    """
    Compute a volatility-targeted position notional with Kelly blend and
    drawdown haircut.

    Parameters
    ----------
    capital_usd:
        Total trading capital (for cap calculation).
    current_equity:
        Current equity (may differ from capital after P&L).
    hwm:
        High-water mark equity (for drawdown calculation).
    realized_vol_pct:
        Instrument's recent annualised volatility as a percentage
        (e.g. 30.0 = 30% annualised vol). Pass 0 if unknown.
    target_vol_pct:
        Desired daily vol contribution per position as a fraction of capital
        (e.g. 1.0 = contribute 1% daily vol).
    win_rate:
        Fraction of trades that were winners (for Kelly scalar).
    avg_win_usd / avg_loss_usd:
        Average absolute USD P&L on wins and losses (payoff ratio).
    kelly_fraction:
        Multiplier applied to the Kelly fraction. Default 0.5 (half-Kelly).
    max_notional_pct:
        Hard cap as fraction of capital. Default 0.25 (25%).
    dd_warn_pct / dd_halt_pct:
        Drawdown thresholds for tapering and halting.
    periods_per_year:
        Used for vol scaling (252 for daily, 365 for crypto).
    """
    if capital_usd <= 0:
        return SizeResult(
            notional_usd=0.0,
            vol_target_notional=0.0,
            kelly_scalar=0.0,
            dd_haircut=0.0,
            realized_vol_pct=realized_vol_pct,
            reject_reason=f"invalid_capital={capital_usd}",
        )

    # 1. Drawdown haircut — computed before anything else
    haircut = _drawdown_haircut(current_equity, hwm, dd_warn_pct, dd_halt_pct)
    if haircut <= _EPS:
        return SizeResult(
            notional_usd=0.0,
            vol_target_notional=0.0,
            kelly_scalar=0.0,
            dd_haircut=0.0,
            realized_vol_pct=realized_vol_pct,
            reject_reason=f"dd_halt: drawdown >= {dd_halt_pct:.0%}",
        )

    # 2. Vol-target sizing: notional = (target_vol / realized_vol) * capital
    eff_vol = max(realized_vol_pct, _MIN_VOL * 100.0)
    # Both target and realized are annualised %; their ratio is dimensionless
    vol_scalar = target_vol_pct / eff_vol
    vol_target_notional = vol_scalar * capital_usd

    # 3. Kelly scalar
    ks = _kelly_scalar(win_rate, avg_win_usd, avg_loss_usd) * kelly_fraction
    # If Kelly gives 0 but we have no history, fall back to 1 (ignore Kelly)
    if ks <= _EPS and win_rate == 0.50 and avg_win_usd == avg_loss_usd:
        ks = kelly_fraction  # symmetric default → half-Kelly = 0.5

    # 4. Apply Kelly and haircut
    notional = vol_target_notional * ks * haircut

    # 5. Hard cap
    max_notional = capital_usd * max_notional_pct
    notional = min(notional, max_notional)

    if notional < _EPS:
        return SizeResult(
            notional_usd=0.0,
            vol_target_notional=vol_target_notional,
            kelly_scalar=ks,
            dd_haircut=haircut,
            realized_vol_pct=realized_vol_pct,
            reject_reason="computed_notional_too_small",
        )

    log.debug(
        "vol_target_sizer",
        notional=round(notional, 2),
        vol_scalar=round(vol_scalar, 4),
        kelly_scalar=round(ks, 4),
        dd_haircut=round(haircut, 4),
    )

    return SizeResult(
        notional_usd=round(notional, 2),
        vol_target_notional=vol_target_notional,
        kelly_scalar=ks,
        dd_haircut=haircut,
        realized_vol_pct=realized_vol_pct,
        reject_reason="",
    )


def vol_target_size_from_returns(
    returns: list[float],
    capital_usd: float,
    current_equity: float,
    hwm: float,
    **kwargs,
) -> SizeResult:
    """
    Convenience wrapper: compute realized vol from a list of daily returns
    before calling vol_target_size().

    Parameters
    ----------
    returns:
        Daily P&L returns as fractions (e.g. 0.01 = 1%).
    capital_usd / current_equity / hwm:
        As in vol_target_size().
    **kwargs:
        Forwarded to vol_target_size().
    """
    periods_per_year = kwargs.get("periods_per_year", 252)
    vol = _annualised_vol(returns, periods_per_year=periods_per_year)
    return vol_target_size(
        capital_usd=capital_usd,
        current_equity=current_equity,
        hwm=hwm,
        realized_vol_pct=vol,
        **kwargs,
    )
