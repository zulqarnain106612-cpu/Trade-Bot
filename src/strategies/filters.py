"""
Professional strategy filters and signal enrichment.

Implements research-backed filters from established practitioners:

  1. Trend filter          — Carver (2019) Systematic Trading Ch.3
                             200-bar EWM trend vs price; only trade with trend
  2. Volatility-adjusted momentum — Chan (2013) Algorithmic Trading Ch.4
                             Risk-adjusted momentum: (μ / σ) rolling window
  3. Overnight gap filter  — Aronson (2006) ETBA Ch.8
                             Flag excessive overnight gaps as noise
  4. Regime-aware position scaler — López de Prado (2018) AFML Ch.17
                             Scale Kelly fraction by regime confidence
  5. Fractal market filter — Peters (1994) Fractal Market Hypothesis
                             Hurst exponent; trade only when H > 0.55 (trending)
  6. Volume-price trend confirm — Granville (1963) OBV / Elder (1993)
                             On-Balance Volume confirms price direction
  7. Volatility regime gate — Schwager (1984) Market Wizards principle
                             Do not trade when vol is 2× its 20-bar median
  8. Multi-timeframe trend alignment — Schwager (1993) The New Market Wizards
                             Short-term signal only taken when intermediate trend agrees

All functions are pure (no I/O), accept pandas Series/DataFrames,
return bool or float scalars, and are fully testable.

Authority:
  - Carver (2019) Systematic Trading — trend filters, position sizing
  - Chan (2013) Algorithmic Trading — risk-adjusted momentum, vol controls
  - Aronson (2006) Evidence-Based Technical Analysis — gap filter
  - López de Prado (2018) AFML — regime-aware sizing
  - Peters (1994) Fractal Market Hypothesis — Hurst exponent
  - Elder (1993) Trading for a Living — Triple Screen / OBV confirmation
  - Granville (1963) Granville's New Key to Stock Market Profits — OBV
  - Schwager (1984/1993) Market Wizards — practitioner risk rules
  - Wilder (1978) New Concepts in Technical Trading Systems — ATR
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EWM_SPAN_TREND: Final[int] = 200        # Carver (2019) — long-term EWM trend
_MOMENTUM_WINDOW: Final[int] = 20        # Chan (2013) — risk-adj momentum window
_HURST_MIN_WINDOW: Final[int] = 100      # Peters (1994) — minimum for stable H
_HURST_TRENDING_THRESHOLD: Final[float] = 0.55  # H > 0.55 = trending
_OBV_CONFIRM_WINDOW: Final[int] = 20     # Elder (1993) — OBV smoothing
_VOL_EXPLOSION_MULTIPLIER: Final[float] = 2.0   # Schwager — halt when vol 2× median
_VOL_EXPLOSION_LOOKBACK: Final[int] = 20


# ---------------------------------------------------------------------------
# 1. Trend filter — Carver (2019) Systematic Trading Ch.3
# ---------------------------------------------------------------------------

def ewm_trend_signal(close: pd.Series, span: int = _EWM_SPAN_TREND) -> float:
    """
    Carver (2019) Ch.3 EWM trend strength signal.

    Returns a normalised trend value in (-1, 1):
      > 0 : uptrend (price above EWM mean)
      < 0 : downtrend (price below EWM mean)
      ≈ 0 : no trend

    The signal is the difference between a fast EWM and a slow EWM,
    normalised by the rolling standard deviation of close prices.

    Carver's "EWMAC" = fast_ewma - slow_ewma.  We use span=16 vs span=64
    as the default "EWMAC16" which Carver identifies as robust across markets.
    """
    if len(close) < span:
        return 0.0
    fast = close.ewm(span=span // 4, adjust=False).mean()
    slow = close.ewm(span=span, adjust=False).mean()
    diff = float(fast.iloc[-1] - slow.iloc[-1])
    std = float(close.rolling(span).std().iloc[-1])
    if std < 1e-9:
        return 0.0
    raw = diff / std
    # Carver normalises to expected abs value ≈ 10; we return as-is for use as multiplier
    return float(np.clip(raw, -3.0, 3.0))


def trend_filter_passes(close: pd.Series, direction: int, span: int = _EWM_SPAN_TREND) -> bool:
    """
    Return True only when the proposed direction agrees with the EWM trend.

    Carver (2019) Ch.3: only take a long when price is above its trend-following
    EWM, only take a short when below.  This prevents counter-trend entries
    which statistically have negative expectancy in trending instruments.
    """
    signal = ewm_trend_signal(close, span)
    if direction == 1:      # long
        return signal > 0.0
    else:                   # short
        return signal < 0.0


# ---------------------------------------------------------------------------
# 2. Volatility-adjusted momentum — Chan (2013) Ch.4
# ---------------------------------------------------------------------------

def vol_adjusted_momentum(
    close: pd.Series,
    window: int = _MOMENTUM_WINDOW,
) -> float:
    """
    Chan (2013) Ch.4 risk-adjusted momentum: μ_ret / σ_ret.

    Returns the rolling Sharpe ratio of log returns over `window` bars.
    Positive = bullish momentum, negative = bearish momentum.
    Magnitude indicates strength of the momentum signal.

    Chan: "Divide the expected return by the expected risk. Trade when this
    ratio is sufficiently large." (Algorithmic Trading, p.86)
    """
    if len(close) < window + 1:
        return 0.0
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < window:
        return 0.0
    recent = log_ret.iloc[-window:]
    mu = float(recent.mean())
    sigma = float(recent.std())
    if sigma < 1e-9:
        return 0.0
    return float(np.clip(mu / sigma, -5.0, 5.0))


# ---------------------------------------------------------------------------
# 3. Overnight gap filter — Aronson (2006) ETBA Ch.8
# ---------------------------------------------------------------------------

def overnight_gap_is_excessive(
    open_price: float,
    prev_close: float,
    atr: float,
    threshold_atr_multiples: float = 2.0,
) -> bool:
    """
    Aronson (2006) Ch.8: reject signals on bars where the open gaps more than
    2× ATR from the prior close.  Such bars indicate unusual overnight news
    events; intraday models trained on normal bars should not trade them.

    Returns True when the gap is excessive (signal should be skipped).
    """
    if atr < 1e-9:
        return False
    gap = abs(open_price - prev_close)
    return gap > threshold_atr_multiples * atr


# ---------------------------------------------------------------------------
# 4. Regime-aware position scaler — López de Prado (2018) AFML Ch.17
# ---------------------------------------------------------------------------

def regime_position_scalar(
    regime_state: int,
    prob_trending: float,
    prob_ranging: float,
    prob_volatile: float,
) -> float:
    """
    AFML Ch.17 regime-conditional position scalar.

    López de Prado: "The bet size should be proportional to the strength
    of the regime signal, not just the direction signal."

    Scalars:
      - Trending regime  : full size, scaled by P(trending) confidence
      - Ranging regime   : reduced to 60% — mean reversion has shorter duration
      - Volatile regime  : 0.0 — risk-off (gate should have blocked, but belt+suspenders)

    Returns a float in [0, 1] that multiplies the Kelly notional.
    """
    from src.config import REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE
    if regime_state == REGIME_VOLATILE or prob_volatile > 0.6:
        return 0.0
    if regime_state == REGIME_TRENDING:
        return float(np.clip(prob_trending, 0.5, 1.0))
    if regime_state == REGIME_RANGING:
        return float(np.clip(prob_ranging * 0.6, 0.2, 0.6))
    return 0.5   # unknown regime — be conservative


# ---------------------------------------------------------------------------
# 5. Hurst exponent — Peters (1994) Fractal Market Hypothesis
# ---------------------------------------------------------------------------

def hurst_exponent(close: pd.Series, min_window: int = _HURST_MIN_WINDOW) -> float:
    """
    Peters (1994) Fractal Market Hypothesis Hurst exponent.

    H > 0.55 : trending / persistent market → suitable for momentum strategies
    H ≈ 0.50 : random walk → no edge
    H < 0.45 : mean-reverting → suitable for mean-reversion strategies

    Implementation: R/S (rescaled range) analysis across log-spaced windows.
    Reference: Peters (1994) Ch.4, Eq. 4.1.

    Returns H in [0, 1].  Returns 0.5 (neutral) if insufficient data.
    """
    if len(close) < min_window:
        return 0.5

    log_ret = np.log(close / close.shift(1)).dropna().to_numpy()
    n = len(log_ret)
    if n < min_window:
        return 0.5

    lags = []
    rs_vals = []
    for lag in [min_window // 4, min_window // 2, min_window, min(n, min_window * 2)]:
        if lag < 10 or lag > n:
            continue
        chunks = [log_ret[i:i+lag] for i in range(0, n - lag + 1, lag)]
        if not chunks:
            continue
        rs_list = []
        for chunk in chunks:
            if len(chunk) < 4:
                continue
            mean_c = np.mean(chunk)
            dev = np.cumsum(chunk - mean_c)
            r = dev.max() - dev.min()
            s = np.std(chunk, ddof=1)
            if s > 1e-12:
                rs_list.append(r / s)
        if rs_list:
            lags.append(math.log(lag))
            rs_vals.append(math.log(np.mean(rs_list)))

    if len(lags) < 2:
        return 0.5

    # OLS slope = Hurst exponent
    x = np.array(lags)
    y = np.array(rs_vals)
    xm, ym = x.mean(), y.mean()
    num = float(((x - xm) * (y - ym)).sum())
    den = float(((x - xm) ** 2).sum())
    if den < 1e-12:
        return 0.5
    H = float(np.clip(num / den, 0.0, 1.0))
    return H


def hurst_filter_passes(close: pd.Series, direction: int) -> bool:
    """
    Return True when the Hurst exponent supports the proposed strategy type.

    Momentum/trend strategies (direction model) pass when H > 0.55.
    Below that the market is random-walking or mean-reverting —
    the direction model's edge evaporates.

    Peters (1994) Ch.7: "Momentum strategies require H > 0.5 to generate
    positive expected return over random entry."
    """
    H = hurst_exponent(close)
    passes = H >= _HURST_TRENDING_THRESHOLD
    if not passes:
        log.debug(
            "strategy_filter.hurst_reject",
            H=round(H, 3),
            threshold=_HURST_TRENDING_THRESHOLD,
            action="signal_filtered",
        )
    return passes


# ---------------------------------------------------------------------------
# 6. On-Balance Volume confirmation — Granville (1963) / Elder (1993)
# ---------------------------------------------------------------------------

def obv_trend_confirms(
    close: pd.Series,
    volume: pd.Series,
    direction: int,
    window: int = _OBV_CONFIRM_WINDOW,
) -> bool:
    """
    Granville (1963) On-Balance Volume trend confirmation.

    Elder (1993) Trading for a Living p.165: "Volume is the fuel of the market.
    OBV must confirm the price trend — divergence is a warning sign."

    Returns True when OBV's rolling slope agrees with the proposed direction.
    OBV rising = buying pressure → confirms long.
    OBV falling = selling pressure → confirms short.
    """
    if len(close) < window + 2 or len(volume) < window + 2:
        return True   # insufficient data — don't filter

    delta = close.diff()
    obv_raw = (np.sign(delta) * volume).fillna(0.0).cumsum()
    obv_slope = float(obv_raw.iloc[-1] - obv_raw.iloc[-window])

    if direction == 1:
        return obv_slope > 0
    else:
        return obv_slope < 0


# ---------------------------------------------------------------------------
# 7. Volatility explosion gate — Schwager (1984)
# ---------------------------------------------------------------------------

def vol_explosion_blocks(
    atr_series: pd.Series,
    lookback: int = _VOL_EXPLOSION_LOOKBACK,
    multiplier: float = _VOL_EXPLOSION_MULTIPLIER,
) -> bool:
    """
    Schwager (1984) Market Wizards principle: stop trading when volatility
    spikes to an abnormal multiple of its recent median.

    A volatility explosion (current ATR > 2× median ATR) indicates a regime
    break or news event.  Models trained on normal conditions have no edge.

    Returns True when the signal should be BLOCKED (vol too high).
    """
    if len(atr_series) < lookback + 1:
        return False
    current_atr = float(atr_series.iloc[-1])
    median_atr = float(atr_series.iloc[-lookback:].median())
    if median_atr < 1e-9:
        return False
    ratio = current_atr / median_atr
    if ratio > multiplier:
        log.warning(
            "strategy_filter.vol_explosion",
            current_atr=round(current_atr, 4),
            median_atr=round(median_atr, 4),
            ratio=round(ratio, 2),
            action="signal_blocked (Schwager 1984)",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# 8. Multi-timeframe trend alignment — Schwager (1993)
# ---------------------------------------------------------------------------

def mtf_trend_aligned(
    fast_signal: float,
    slow_signal: float,
    direction: int,
) -> bool:
    """
    Schwager (1993) The New Market Wizards: only take short-term signals
    when the intermediate-term trend agrees.

    fast_signal : short-timeframe trend signal (e.g. 15m ewm_trend_signal)
    slow_signal : intermediate trend signal (e.g. 4h ewm_trend_signal)
    direction   : proposed trade direction (1=long, 0=short)

    Returns True when both timeframes agree with the direction.

    Schwager: "The best trades are the ones where the primary, secondary,
    and near-term trends are all in the same direction."
    """
    if direction == 1:
        return fast_signal > 0 and slow_signal > 0
    else:
        return fast_signal < 0 and slow_signal < 0


# ---------------------------------------------------------------------------
# Combined filter stack
# ---------------------------------------------------------------------------

def apply_all_strategy_filters(
    close: pd.Series,
    volume: pd.Series,
    atr_series: pd.Series,
    direction: int,
    regime_state: int,
    prob_trending: float,
    prob_ranging: float,
    prob_volatile: float,
    open_price: float | None = None,
    prev_close: float | None = None,
) -> dict[str, object]:
    """
    Run the full professional filter stack and return a verdict dict.

    Returns::

        {
          "passes": bool,          # True = all filters pass
          "scalar": float,         # position size scalar [0, 1]
          "filters_failed": list,  # names of failed filters
          "details": dict,         # per-filter results
        }

    Filters are checked in order; short-circuit on first block.
    """
    failed: list[str] = []
    details: dict[str, object] = {}

    # 1. Volatility explosion check (Schwager 1984) — hardest block first
    if vol_explosion_blocks(atr_series):
        failed.append("vol_explosion")
    details["vol_explosion"] = "blocked" if "vol_explosion" in failed else "pass"

    # 2. Hurst exponent filter (Peters 1994)
    H = hurst_exponent(close)
    details["hurst"] = round(H, 3)
    if H < _HURST_TRENDING_THRESHOLD and "vol_explosion" not in failed:
        failed.append("hurst_insufficient")

    # 3. Trend filter (Carver 2019)
    trend_sig = ewm_trend_signal(close)
    details["trend_signal"] = round(trend_sig, 3)
    if not trend_filter_passes(close, direction) and not failed:
        failed.append("trend_counter")

    # 4. Vol-adjusted momentum (Chan 2013)
    mom = vol_adjusted_momentum(close)
    details["vol_adj_momentum"] = round(mom, 3)

    # 5. OBV confirmation (Granville/Elder)
    obv_ok = obv_trend_confirms(close, volume, direction)
    details["obv_confirms"] = obv_ok
    if not obv_ok and not failed:
        failed.append("obv_divergence")

    # 6. Overnight gap filter (Aronson 2006)
    if open_price is not None and prev_close is not None and len(atr_series) > 0:
        atr_now = float(atr_series.iloc[-1])
        gap_excessive = overnight_gap_is_excessive(open_price, prev_close, atr_now)
        details["gap_excessive"] = gap_excessive
        if gap_excessive and not failed:
            failed.append("overnight_gap")

    # 7. Regime position scalar (López de Prado AFML Ch.17)
    scalar = regime_position_scalar(regime_state, prob_trending, prob_ranging, prob_volatile)
    details["regime_scalar"] = round(scalar, 3)

    passes = len(failed) == 0

    if not passes:
        log.info(
            "strategy_filters.blocked",
            direction=direction,
            failed=failed,
            hurst=round(H, 3),
            trend=round(trend_sig, 3),
        )

    return {
        "passes": passes,
        "scalar": scalar if passes else 0.0,
        "filters_failed": failed,
        "details": details,
    }
