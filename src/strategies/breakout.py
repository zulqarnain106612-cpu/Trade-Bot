"""
Donchian-channel breakout strategy signals.

Generates entry signals when price breaks above the N-bar high (long) or
below the N-bar low (short), gated by an ATR-based volatility filter to
avoid breakouts during abnormally compressed or expanded regimes.

Two functions:

  1. donchian_signal() — N-bar Donchian channel with a separate exit window.
  2. breakout_signal() — combined signal: Donchian entry + ATR volatility
     gate (reject when ATR/price is outside [min_atr_pct, max_atr_pct]).

All functions are pure, accept numpy arrays, and return BreakoutSignal
frozen dataclasses.

Authority:
  Donchian (1960) "Trend Following Methods in Commodity Price Analysis" —
    original N-week channel breakout system.
  Covel (2004) "Trend Following" Ch.4 — Turtle-derived Donchian channels.
  Wilder (1978) "New Concepts in Technical Trading Systems" — ATR definition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_ENTRY_PERIOD: Final[int] = 20  # N bars for breakout detection
_DEFAULT_EXIT_PERIOD: Final[int] = 10  # shorter exit channel (Turtle system)
_DEFAULT_ATR_PERIOD: Final[int] = 14  # Wilder ATR
_DEFAULT_MIN_ATR_PCT: Final[float] = 0.1  # 0.1% min ATR/price — avoids dead markets
_DEFAULT_MAX_ATR_PCT: Final[float] = 10.0  # 10% max ATR/price — avoids blow-up regimes

_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakoutSignal:
    """
    Output of a Donchian-channel breakout evaluation.

    direction:  +1 = long (price broke above upper channel)
               -1 = short (price broke below lower channel)
                0 = no signal
    """

    direction: int  # -1, 0, +1
    is_entry: bool  # True when channel is broken
    is_exit: bool  # True when price reverts into exit channel
    upper_channel: float  # N-bar high used for this evaluation
    lower_channel: float  # N-bar low
    exit_upper: float  # exit-window high
    exit_lower: float  # exit-window low
    atr: float  # current ATR (0 if not computed)
    atr_pct: float  # ATR / latest_price * 100
    confidence: float  # [0, 1] — how far price penetrated the channel
    reject_reason: str  # non-empty when direction == 0 due to filter


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------


def _compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    """
    Wilder ATR over the last ``period`` bars.

    True Range = max(H-L, |H-C_prev|, |L-C_prev|)
    Wilder smoothing: EMA with alpha = 1/period.
    """
    n = len(closes)
    if n < 2 or period < 1:
        return 0.0

    trs = []
    for i in range(1, n):
        hl = float(highs[i]) - float(lows[i])
        hc = abs(float(highs[i]) - float(closes[i - 1]))
        lc = abs(float(lows[i]) - float(closes[i - 1]))
        trs.append(max(hl, hc, lc))

    if not trs:
        return 0.0

    # Wilder EMA (initial value = simple average of first `period` TRs)
    alpha = 1.0 / period
    window = trs[:period]
    atr = sum(window) / len(window)
    for tr in trs[period:]:
        atr = alpha * tr + (1.0 - alpha) * atr
    return atr


# ---------------------------------------------------------------------------
# Donchian channel signal
# ---------------------------------------------------------------------------


def donchian_signal(
    closes: np.ndarray,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
    entry_period: int = _DEFAULT_ENTRY_PERIOD,
    exit_period: int = _DEFAULT_EXIT_PERIOD,
) -> BreakoutSignal:
    """
    Compute a Donchian-channel breakout signal.

    Parameters
    ----------
    closes:
        1D close price array, chronological.
    highs / lows:
        Optional high/low arrays. If omitted, closes are used as proxies.
    entry_period:
        Lookback for the entry channel (e.g. 20-bar high/low).
    exit_period:
        Lookback for the exit channel (shorter = tighter exit). Must be
        <= entry_period.

    Returns
    -------
    BreakoutSignal
    """
    n = len(closes)
    if n < entry_period + 1:
        return _no_signal(
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            f"insufficient_bars={n} < entry_period+1={entry_period + 1}",
        )

    if highs is None:
        highs = closes
    if lows is None:
        lows = closes

    # Exclude the last bar (current) from channel calculation
    channel_h = np.asarray(highs, dtype=float)
    channel_l = np.asarray(lows, dtype=float)
    ch_closes = np.asarray(closes, dtype=float)

    upper = float(np.max(channel_h[-(entry_period + 1) : -1]))
    lower = float(np.min(channel_l[-(entry_period + 1) : -1]))

    ep = min(exit_period, entry_period)
    exit_upper = float(np.max(channel_h[-(ep + 1) : -1]))
    exit_lower = float(np.min(channel_l[-(ep + 1) : -1]))

    latest = float(ch_closes[-1])

    is_long_entry = latest > upper
    is_short_entry = latest < lower
    is_entry = is_long_entry or is_short_entry
    is_exit = exit_lower <= latest <= exit_upper

    direction = 0
    if is_long_entry:
        direction = 1
    elif is_short_entry:
        direction = -1

    # Confidence: how far did price penetrate beyond the channel?
    channel_width = max(upper - lower, _EPS)
    if direction == 1:
        confidence = min((latest - upper) / channel_width, 1.0)
    elif direction == -1:
        confidence = min((lower - latest) / channel_width, 1.0)
    else:
        confidence = 0.0

    return BreakoutSignal(
        direction=direction,
        is_entry=is_entry,
        is_exit=is_exit,
        upper_channel=upper,
        lower_channel=lower,
        exit_upper=exit_upper,
        exit_lower=exit_lower,
        atr=0.0,
        atr_pct=0.0,
        confidence=confidence,
        reject_reason=""
        if is_entry
        else f"price={latest:.4f} within channel [{lower:.4f}, {upper:.4f}]",
    )


# ---------------------------------------------------------------------------
# Combined signal: Donchian + ATR gate
# ---------------------------------------------------------------------------


def breakout_signal(
    closes: np.ndarray,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
    entry_period: int = _DEFAULT_ENTRY_PERIOD,
    exit_period: int = _DEFAULT_EXIT_PERIOD,
    atr_period: int = _DEFAULT_ATR_PERIOD,
    min_atr_pct: float = _DEFAULT_MIN_ATR_PCT,
    max_atr_pct: float = _DEFAULT_MAX_ATR_PCT,
) -> BreakoutSignal:
    """
    Full breakout signal: Donchian entry gate + ATR volatility filter.

    ATR gate rejects:
      - ATR/price < min_atr_pct  (dead/compressed market, false breakout risk)
      - ATR/price > max_atr_pct  (crisis/spike, risk too high)

    Parameters
    ----------
    closes / highs / lows:
        OHLC arrays. If highs/lows are omitted, closes are used for both.
    entry_period:
        Donchian channel lookback for entries.
    exit_period:
        Donchian channel lookback for exits (shorter window).
    atr_period:
        Wilder ATR period.
    min_atr_pct / max_atr_pct:
        ATR/price (%) bounds; outside this range the signal is suppressed.
    """
    if highs is None:
        highs = closes
    if lows is None:
        lows = closes

    h = np.asarray(highs, dtype=float)
    l_ = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)

    base = donchian_signal(c, h, l_, entry_period=entry_period, exit_period=exit_period)

    atr = _compute_atr(h, l_, c, atr_period)
    latest = float(c[-1])
    atr_pct = atr / max(latest, _EPS) * 100.0

    if not math.isfinite(atr_pct):
        atr_pct = 0.0
        atr = 0.0

    if base.is_entry:
        if atr_pct < min_atr_pct:
            reason = f"atr_too_low={atr_pct:.3f}% < min={min_atr_pct}%"
            log.debug("breakout.atr_gate_low", atr_pct=atr_pct)
            return _no_signal(
                base.upper_channel,
                base.lower_channel,
                base.exit_upper,
                base.exit_lower,
                atr,
                atr_pct,
                reason,
            )
        if atr_pct > max_atr_pct:
            reason = f"atr_too_high={atr_pct:.3f}% > max={max_atr_pct}%"
            log.debug("breakout.atr_gate_high", atr_pct=atr_pct)
            return _no_signal(
                base.upper_channel,
                base.lower_channel,
                base.exit_upper,
                base.exit_lower,
                atr,
                atr_pct,
                reason,
            )

    return BreakoutSignal(
        direction=base.direction,
        is_entry=base.is_entry,
        is_exit=base.is_exit,
        upper_channel=base.upper_channel,
        lower_channel=base.lower_channel,
        exit_upper=base.exit_upper,
        exit_lower=base.exit_lower,
        atr=atr,
        atr_pct=atr_pct,
        confidence=base.confidence,
        reject_reason=base.reject_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_signal(
    upper: float,
    lower: float,
    exit_upper: float,
    exit_lower: float,
    atr: float,
    atr_pct: float,
    reason: str,
) -> BreakoutSignal:
    return BreakoutSignal(
        direction=0,
        is_entry=False,
        is_exit=False,
        upper_channel=upper,
        lower_channel=lower,
        exit_upper=exit_upper,
        exit_lower=exit_lower,
        atr=atr,
        atr_pct=atr_pct,
        confidence=0.0,
        reject_reason=reason,
    )
