"""
Feature engineering pipeline.

Implements every feature from the signal architecture spec:
  1. Fractional differentiation (d=0.4)          — AFML Ch.5
  2. VWAP deviation z-score                       — price microstructure
  3. Order Flow Imbalance (OFI)                   — Cont et al. (2014)
  4. Realized volatility ratio (short / long)     — Chan (2013)
  5. ATR momentum                                 — Wilder (1978)
  6. Rolling Sharpe                               — Kelly (1956) / Chan (2013)
  7. Volume z-score                               — standardized volume pressure
  8. Triple-barrier labeling                      — AFML Ch.3
  9. Meta-label targets (bet-or-not column)       — AFML Ch.4

Authority sources:
  - López de Prado (2018) AFML Ch.3-5
  - Cont, Kukanov & Stoikov (2014) "The Price Impact of Order Book Events"
  - Chan (2013) Algorithmic Trading — realized vol, ATR momentum
  - Wilder (1978) New Concepts in Technical Trading Systems — ATR
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np
import pandas as pd
import structlog

from src.config import FeatureSettings
from src.tuning.live_overrides import effective_feature_settings


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column name constants — shared with trainer and signal engine
# ---------------------------------------------------------------------------

COL_FRAC_DIFF: Final[str] = "frac_diff"
COL_VWAP_DEV: Final[str] = "vwap_dev_zscore"
COL_OFI: Final[str] = "ofi"
COL_REALIZED_VOL_RATIO: Final[str] = "realized_vol_ratio"
COL_ATR_MOMENTUM: Final[str] = "atr_momentum"
COL_ROLLING_SHARPE: Final[str] = "rolling_sharpe"
COL_VOLUME_ZSCORE: Final[str] = "volume_zscore"

# All feature columns in canonical order — used by trainer for consistent X matrix.
#
# GAP-015: INTELLIGENCE_FEATURE_COLUMNS are defined separately in
# src/features/intelligence_features.py.  The trainer resolves the active
# column set at runtime via get_active_feature_columns() below, which
# reads coverage from the DB and drops columns below the threshold.
# This list (BASE_FEATURE_COLUMNS) is the fallback when no intelligence
# history is available.
BASE_FEATURE_COLUMNS: Final[list[str]] = [
    COL_FRAC_DIFF,
    COL_VWAP_DEV,
    COL_OFI,
    COL_REALIZED_VOL_RATIO,
    COL_ATR_MOMENTUM,
    COL_ROLLING_SHARPE,
    COL_VOLUME_ZSCORE,
]

# Backward-compat alias: existing imports of FEATURE_COLUMNS still work.
# New code that needs the full 25-feature set should call
# get_active_feature_columns() instead.
FEATURE_COLUMNS: Final[list[str]] = BASE_FEATURE_COLUMNS


def get_active_feature_columns(
    coverage: dict[str, float] | None = None,
    min_coverage: float = 0.6,
) -> list[str]:
    """
    GAP-015 Step 4: Return the ordered column list for the current training run.

    Starts with BASE_FEATURE_COLUMNS (7 core features), then appends any
    intelligence columns whose coverage fraction meets the threshold.

    Args:
        coverage:     Output of storage.intelligence_feature_coverage()['coverage'].
                      None or empty → return BASE_FEATURE_COLUMNS only (7-feature mode).
        min_coverage: Minimum non-NULL fraction [0,1] to include a column.

    Returns:
        Ordered list of column names.  Always starts with the 7 base features.
        May include up to 18 additional intelligence columns.

    Example:
        coverage = await storage.intelligence_feature_coverage("BTCUSDT", "1h")
        cols = get_active_feature_columns(coverage["coverage"], min_coverage=0.6)
        # trainer then uses: X = df[cols].to_numpy()
    """
    import structlog as _sl

    from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

    _log = _sl.get_logger(__name__)

    if not coverage:
        _log.info(
            "get_active_feature_columns",
            mode="7-feature",
            reason="no intelligence coverage data",
        )
        return list(BASE_FEATURE_COLUMNS)

    included = []
    excluded = []
    for col in INTELLIGENCE_FEATURE_COLUMNS:
        frac = coverage.get(col, 0.0)
        if frac >= min_coverage:
            included.append(col)
        else:
            excluded.append((col, frac))

    if excluded:
        _log.warning(
            "get_active_feature_columns_excluded",
            excluded=[(c, f"{f * 100:.1f}%") for c, f in excluded],
            threshold=f"{min_coverage * 100:.0f}%",
        )

    active = list(BASE_FEATURE_COLUMNS) + included
    _log.info(
        "get_active_feature_columns",
        total=len(active),
        base=len(BASE_FEATURE_COLUMNS),
        intelligence=len(included),
        mode=f"{len(active)}-feature",
    )
    return active


# Label columns
COL_LABEL: Final[str] = "label"
COL_META_LABEL: Final[str] = "meta_label"
COL_RETURN: Final[str] = "log_return"

# Required input OHLCV columns
_REQ_COLS: Final[frozenset[str]] = frozenset({"open", "high", "low", "close", "volume"})


# ---------------------------------------------------------------------------
# Fractional differentiation — AFML Ch.5
# ---------------------------------------------------------------------------


_FRAC_DIFF_MAX_WINDOW: Final[int] = 200  # AFML p.82 fixed-width window cap


def _frac_diff_weights(
    d: float,
    size: int,
    threshold: float,
    max_window: int = _FRAC_DIFF_MAX_WINDOW,
) -> np.ndarray:
    """
    Compute fractional differentiation weights via binomial series expansion.

    w_k = product_{j=0}^{k-1} (d - j) / (j + 1)

    Stops at the FIRST of:
      (a) |w_k| < threshold  — weights below significance floor, OR
      (b) k == max_window    — fixed-width window cap (AFML p.82).

    For d=0.4 with threshold=1e-5 the uncapped window reaches ~1 458 bars —
    far beyond any realistic lookback.  Capping at max_window=200 retains
    95%+ of the long-memory signal while keeping burn-in practical.
    """
    cap = min(size, max_window)
    w = [1.0]
    for k in range(1, cap):
        w_k = -w[-1] * (d - k + 1) / k
        if abs(w_k) < threshold:
            break
        w.append(w_k)
    return np.array(w[::-1])  # oldest weight first


def fractional_differentiation(
    series: pd.Series,
    d: float,
    threshold: float,
    max_window: int = _FRAC_DIFF_MAX_WINDOW,
) -> pd.Series:
    """
    Apply fractional differentiation to a price series.

    AFML Ch.5 — fixed-width window variant.  Produces a stationary series
    that retains long-memory: more informative than integer-differenced returns.

    Parameters
    ----------
    series     : raw price series (e.g. close prices), float64
    d          : differentiation order in (0, 1); spec uses d=0.4
    threshold  : weight significance cutoff
    max_window : hard cap on effective window length (AFML p.82)

    Returns
    -------
    pd.Series of same index, NaN for initial rows inside the weight window.
    """
    weights = _frac_diff_weights(d, len(series), threshold, max_window)
    width = len(weights)
    if width > len(series):
        return pd.Series(np.nan, index=series.index, dtype=np.float64)

    values = series.to_numpy(dtype=np.float64)
    n = len(values)
    # NEW-007: replace Python loop with np.convolve — single C-level call,
    # 100x+ faster than `for i in range(n): np.dot(weights, window)`.
    # weights are already in oldest-first order; convolve with reversed kernel
    # is equivalent to a sliding dot-product (correlation mode).
    conv = np.convolve(values, weights[::-1], mode="full")
    # 'full' output length is n + width - 1; slice to align with original index.
    # First valid output sits at index (width - 1); keep only n values.
    result = np.full(n, np.nan, dtype=np.float64)
    valid_start = width - 1
    result[valid_start:] = conv[width - 1 : width - 1 + (n - valid_start)]
    return pd.Series(result, index=series.index, dtype=np.float64)


# ---------------------------------------------------------------------------
# VWAP deviation z-score
# ---------------------------------------------------------------------------


def vwap_deviation_zscore(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    window: int,
) -> pd.Series:
    """
    Rolling VWAP deviation z-score.

    VWAP = sum(typical_price * volume) / sum(volume)  over rolling window.
    deviation = (close - VWAP) / VWAP
    z-score   = (deviation - rolling_mean(deviation)) / rolling_std(deviation)

    Positive z-score → price above VWAP (bullish short-term pressure).
    """
    typical_price = (high + low + close) / 3.0
    tp_vol = typical_price * volume

    vwap = (
        tp_vol.rolling(window, min_periods=window).sum()
        / volume.rolling(window, min_periods=window).sum()
    )

    deviation = (close - vwap) / vwap.replace(0.0, np.nan)
    z = (deviation - deviation.rolling(window, min_periods=window).mean()) / (
        deviation.rolling(window, min_periods=window).std(ddof=1).replace(0.0, np.nan)
    )
    return z.rename(COL_VWAP_DEV)


# ---------------------------------------------------------------------------
# Order Flow Imbalance (OFI) — rolling proxy from OHLCV
# ---------------------------------------------------------------------------


def order_flow_imbalance(
    close: pd.Series,
    volume: pd.Series,
    window: int,
) -> pd.Series:
    """
    Proxy OFI computed from OHLCV when live order-book data is unavailable.

    OFI_bar = sign(delta_close) * volume  (signed volume pressure per bar).
    Rolling OFI = rolling_sum(OFI_bar) normalised by rolling_sum(volume).

    Range [-1, 1].  Positive → net buying pressure over window.

    When live order-book snapshots are available the signal engine supplements
    this with the real-time OFI from OrderBookSnapshot.order_flow_imbalance().
    """
    delta_close = close.diff()
    direction = np.sign(delta_close)
    ofi_bar = direction * volume
    rolling_ofi = ofi_bar.rolling(window, min_periods=window).sum()
    rolling_vol = volume.rolling(window, min_periods=window).sum().replace(0.0, np.nan)
    return (rolling_ofi / rolling_vol).rename(COL_OFI)


# ---------------------------------------------------------------------------
# Realized volatility ratio
# ---------------------------------------------------------------------------


def realized_vol_ratio(
    close: pd.Series,
    short_window: int,
    long_window: int,
) -> pd.Series:
    """
    Ratio of short-window to long-window realized volatility.

    rv_short = std(log_returns, short_window) * sqrt(short_window)
    rv_long  = std(log_returns, long_window)  * sqrt(long_window)
    ratio    = rv_short / rv_long

    ratio > 1 → volatility spiking (recent bars more volatile than baseline).
    ratio < 1 → volatility compressing (consolidation).

    Based on Chan (2013) Ch.3 volatility regime identification.
    """
    log_ret = np.log(close / close.shift(1))
    rv_short = log_ret.rolling(short_window, min_periods=short_window).std(ddof=1) * np.sqrt(
        short_window
    )
    rv_long = log_ret.rolling(long_window, min_periods=long_window).std(ddof=1) * np.sqrt(
        long_window
    )
    ratio = (rv_short / rv_long.replace(0.0, np.nan)).rename(COL_REALIZED_VOL_RATIO)
    return ratio


# ---------------------------------------------------------------------------
# ATR momentum — Wilder (1978)
# ---------------------------------------------------------------------------


def atr_momentum(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
) -> pd.Series:
    """
    ATR-normalised price momentum.

    ATR = Wilder EMA of true range over `window` bars.
    momentum = (close - close.shift(window)) / ATR

    Normalising by ATR makes the signal comparable across different
    volatility regimes — a core property required by the meta-label gate.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing: alpha = 1/window
    atr = tr.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    mom = (close - close.shift(window)) / atr.replace(0.0, np.nan)
    return mom.rename(COL_ATR_MOMENTUM)


# ---------------------------------------------------------------------------
# Rolling Sharpe
# ---------------------------------------------------------------------------


def rolling_sharpe(
    close: pd.Series,
    window: int,
) -> pd.Series:
    """
    Rolling annualised Sharpe ratio over `window` bars.

    Sharpe = mean(log_ret) / std(log_ret) * sqrt(window)

    Uses log returns, ddof=1 standard deviation.
    Returns NaN for rows inside the burn-in period.

    Reference: Kelly (1956), Chan (2013) Ch.1 performance metrics.
    """
    log_ret = np.log(close / close.shift(1))
    mu = log_ret.rolling(window, min_periods=window).mean()
    sigma = log_ret.rolling(window, min_periods=window).std(ddof=1).replace(0.0, np.nan)
    sharpe = (mu / sigma) * np.sqrt(window)
    return sharpe.rename(COL_ROLLING_SHARPE)


# ---------------------------------------------------------------------------
# Volume z-score
# ---------------------------------------------------------------------------


def volume_zscore(
    volume: pd.Series,
    window: int,
) -> pd.Series:
    """
    Rolling z-score of volume.

    z = (volume - rolling_mean) / rolling_std

    Spikes signal unusual participation.  Used alongside OFI to distinguish
    directional from noise volume.
    """
    mu = volume.rolling(window, min_periods=window).mean()
    sigma = volume.rolling(window, min_periods=window).std(ddof=1).replace(0.0, np.nan)
    z = (volume - mu) / sigma
    return z.rename(COL_VOLUME_ZSCORE)


# ---------------------------------------------------------------------------
# Triple-barrier labeling — AFML Ch.3
# ---------------------------------------------------------------------------


@dataclass
class TripleBarrierResult:
    """
    Triple-barrier label for a single observation.

    label       : 1 = profit-take hit, 0 = stop-loss hit, -1 = time exit
    exit_index  : positional index in the bar array where exit occurred
    exit_reason : "profit_target" | "stop_loss" | "time_exit"
    """

    label: int
    exit_index: int
    exit_reason: str


def _compute_daily_vol(log_returns: pd.Series, span: int = 63) -> pd.Series:
    """
    EWMA estimate of daily volatility from log returns.

    AFML p.44 — uses span=63 bars (≈ 1 trading month) for EWM.
    """
    return log_returns.ewm(span=span, min_periods=span).std()


def triple_barrier_labels(
    close: pd.Series,
    pt_multiplier: float,
    sl_multiplier: float,
    max_holding: int,
    daily_vol: pd.Series | None = None,
) -> pd.Series:
    """
    Apply triple-barrier labeling to a close price series.

    For each bar t:
      - Upper barrier: close[t] * (1 + pt_multiplier * vol[t])
      - Lower barrier: close[t] * (1 - sl_multiplier * vol[t])
      - Time barrier:  t + max_holding bars

    Label = 1  if upper hit first (profit-take)
    Label = 0  if lower hit first (stop-loss)
    Label = -1 if time barrier reached first (time-exit)

    vol is per-bar daily_vol (EWMA); if not supplied, computed internally.

    AFML Ch.3 — triple-barrier method with dynamic barrier widths.

    Parameters
    ----------
    close         : close price series
    pt_multiplier : profit-take barrier = vol * pt_multiplier
    sl_multiplier : stop-loss barrier   = vol * sl_multiplier
    max_holding   : maximum bars to hold before time-exit
    daily_vol     : pre-computed vol series (optional)

    Returns
    -------
    pd.Series of int labels aligned to close.index, NaN at tail where
    the full holding window extends beyond available data.
    """
    prices = close.to_numpy(dtype=np.float64)
    n = len(prices)

    if daily_vol is None:
        log_ret = np.log(close / close.shift(1)).fillna(0.0)
        daily_vol = _compute_daily_vol(log_ret)

    vols = daily_vol.to_numpy(dtype=np.float64)
    labels = np.full(n, np.nan, dtype=np.float64)

    # VF-015: Replace O(n x max_holding) Python double-loop with a vectorized
    # approach.  For each look-ahead offset k in [1, max_holding]:
    #   - build shifted price arrays (np.roll / slicing)
    #   - compare against per-row upper/lower barriers
    #   - record the FIRST offset at which a barrier is hit
    # Total work: max_holding array passes over n elements — all in NumPy C.
    # At n=10 000 and max_holding=60 this is ~100x faster than the Python loop.

    valid_mask = ~np.isnan(vols) & (np.abs(vols) >= 1e-10)
    entry_prices = prices.copy()
    upper = np.where(valid_mask, entry_prices * (1.0 + pt_multiplier * vols), np.nan)
    lower = np.where(valid_mask, entry_prices * (1.0 - sl_multiplier * vols), np.nan)

    # Initialize all valid rows as time-exit (-1)
    labels[valid_mask] = -1.0

    # first_hit[t] tracks the earliest offset k at which a barrier was hit.
    # We iterate k from 1 to max_holding and update only rows not yet resolved.
    resolved = ~valid_mask  # rows already finalized (invalid or already hit)

    for k in range(1, max_holding + 1):
        if resolved.all():
            break
        # future_prices[t] = prices[t + k], with NaN where t + k >= n
        future_idx = np.arange(n) + k
        in_bounds = future_idx < n
        future_prices = np.where(
            in_bounds,
            prices[np.minimum(future_idx, n - 1)],
            np.nan,
        )
        hit_upper = in_bounds & ~resolved & (future_prices >= upper)
        hit_lower = in_bounds & ~resolved & (future_prices <= lower)
        labels[hit_upper] = 1.0
        labels[hit_lower] = 0.0
        resolved |= hit_upper | hit_lower

    return pd.Series(labels, index=close.index, dtype=np.float64).rename(COL_LABEL)


# ---------------------------------------------------------------------------
# Meta-label targets — AFML Ch.4
# ---------------------------------------------------------------------------


def meta_labels(
    primary_signal: pd.Series,
    realized_labels: pd.Series,
) -> pd.Series:
    """
    Generate meta-label targets from a primary signal and realized outcomes.

    meta_label = 1 when the primary signal agrees with the realized outcome.
    meta_label = 0 otherwise (primary was wrong or timed out).

    Agreement:
      primary_signal = 1 (long)  AND realized_label = 1  → 1
      primary_signal = 0 (short) AND realized_label = 0  → 1
      time-exit (label = -1)                             → 0

    AFML Ch.4 — meta-labeling trains a second classifier on top of the
    primary to filter out low-confidence primary signals.

    Parameters
    ----------
    primary_signal  : series of primary direction signals (1=long, 0=short)
    realized_labels : triple-barrier labels (1, 0, -1)

    Returns
    -------
    pd.Series of int (0 or 1) meta-labels.
    """
    ml = pd.Series(0, index=primary_signal.index, dtype=np.int8)
    agree_long = (primary_signal == 1) & (realized_labels == 1)
    agree_short = (primary_signal == 0) & (realized_labels == 0)
    ml[agree_long | agree_short] = 1
    return ml.rename(COL_META_LABEL)


# ---------------------------------------------------------------------------
# Pipeline result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FeatureMatrix:
    """
    Result of a full pipeline run.

    features   : DataFrame with all 7 feature columns, NaN rows dropped
    labels     : triple-barrier label series (aligned to features index)
    meta        : meta-label series (aligned to features index)
    daily_vol   : per-bar volatility series (used for barrier width)
    log_returns : log return series (used by trainer for sample weights)
    """

    features: pd.DataFrame
    labels: pd.Series
    # SCAN2-011: meta-labels are computed post-direction-model fit in ModelTrainer.
    # This field is None out of build_feature_matrix(); trainer.py populates it.
    meta: pd.Series | None
    daily_vol: pd.Series
    log_returns: pd.Series
    dropped_rows: int = field(default=0)


# ---------------------------------------------------------------------------
# Full pipeline entry point
# ---------------------------------------------------------------------------


def build_feature_matrix(
    bars: pd.DataFrame,
    ofi_snapshots: pd.Series | None = None,
    cfg: FeatureSettings | None = None,
) -> FeatureMatrix:
    """
    Build the complete feature matrix from a DataFrame of OHLCV bars.

    Parameters
    ----------
    bars            : DataFrame with columns open/high/low/close/volume,
                      indexed by integer Unix-ms timestamps (ascending).
    ofi_snapshots   : optional live OFI series (same index as bars).
                      When provided, replaces the OHLCV-derived OFI proxy.
    cfg             : FeatureSettings; if None, loaded from global settings.

    Returns
    -------
    FeatureMatrix — all arrays aligned to the same post-dropna index.

    Raises
    ------
    ValueError  : if bars is missing required columns or has fewer than
                  min_required rows for the widest rolling window.
    """
    if cfg is None:
        cfg = effective_feature_settings()

    missing = _REQ_COLS - set(bars.columns)
    if missing:
        raise ValueError(f"bars DataFrame missing required columns: {missing}")

    n_input = len(bars)
    min_required = max(
        cfg.vwap_window,
        cfg.ofi_window,
        cfg.realized_vol_window_long,
        cfg.atr_window,
        cfg.sharpe_window,
        cfg.volume_zscore_window,
        # VF-016: include the frac-diff weight window so the min-rows guard
        # accounts for the 200-bar NaN burn-in, preventing silent row loss.
        _FRAC_DIFF_MAX_WINDOW,
        cfg.triple_barrier_max_holding_bars + 63,  # 63 = EWMA vol warmup
    )
    if n_input < min_required:
        raise ValueError(f"bars has {n_input} rows; need at least {min_required} for all windows")

    close = bars["close"].astype(np.float64)
    high = bars["high"].astype(np.float64)
    low = bars["low"].astype(np.float64)
    volume = bars["volume"].astype(np.float64)

    # M-13: detect flat-price runs — >5% of bars with zero price delta indicates
    # exchange halt, stale feed, or data gap. Features (frac-diff, vol-ratio,
    # ATR) will be distorted; warn so the operator can investigate.
    _flat_count = int((close.diff().abs() < 1e-10).sum())
    if _flat_count > len(close) * 0.05:
        log.warning(
            "pipeline.flat_price_detected",
            flat_bar_count=_flat_count,
            pct=round(_flat_count / len(close) * 100, 1),
            possible_cause="exchange_halt_or_stale_data_feed",
            action="features_computed_but_signal_quality_degraded",
        )

    log_ret = np.log(close / close.shift(1))

    # ------------------------------------------------------------------ #
    # 1. Fractional differentiation (AFML Ch.5)
    # ------------------------------------------------------------------ #
    fd = fractional_differentiation(
        close,
        d=cfg.frac_diff_d,
        threshold=cfg.frac_diff_threshold,
        max_window=_FRAC_DIFF_MAX_WINDOW,
    )

    # ------------------------------------------------------------------ #
    # 2. VWAP deviation z-score
    # ------------------------------------------------------------------ #
    vwap_dev = vwap_deviation_zscore(high, low, close, volume, window=cfg.vwap_window)

    # ------------------------------------------------------------------ #
    # 3. OFI — live snapshot override or OHLCV proxy
    # ------------------------------------------------------------------ #
    if ofi_snapshots is not None:
        ofi_series = ofi_snapshots.reindex(bars.index).rename(COL_OFI)
    else:
        ofi_series = order_flow_imbalance(close, volume, window=cfg.ofi_window)

    # ------------------------------------------------------------------ #
    # 4. Realized volatility ratio
    # ------------------------------------------------------------------ #
    rv_ratio = realized_vol_ratio(
        close,
        short_window=cfg.realized_vol_window_short,
        long_window=cfg.realized_vol_window_long,
    )

    # ------------------------------------------------------------------ #
    # 5. ATR momentum
    # ------------------------------------------------------------------ #
    atr_mom = atr_momentum(high, low, close, window=cfg.atr_window)

    # ------------------------------------------------------------------ #
    # 6. Rolling Sharpe
    # ------------------------------------------------------------------ #
    r_sharpe = rolling_sharpe(close, window=cfg.sharpe_window)

    # ------------------------------------------------------------------ #
    # 7. Volume z-score
    # ------------------------------------------------------------------ #
    vol_z = volume_zscore(volume, window=cfg.volume_zscore_window)

    # ------------------------------------------------------------------ #
    # 8. Daily vol — shared by triple-barrier + trainer sample weights
    # ------------------------------------------------------------------ #
    daily_vol = _compute_daily_vol(log_ret.fillna(0.0))

    # ------------------------------------------------------------------ #
    # 9. Triple-barrier labels (AFML Ch.3)
    # ------------------------------------------------------------------ #
    tb_labels = triple_barrier_labels(
        close,
        pt_multiplier=cfg.triple_barrier_pt_multiplier,
        sl_multiplier=cfg.triple_barrier_sl_multiplier,
        max_holding=cfg.triple_barrier_max_holding_bars,
        daily_vol=daily_vol,
    )

    # ------------------------------------------------------------------ #
    # Assemble feature matrix — drop any row with NaN in features or label
    # ------------------------------------------------------------------ #
    feature_df = pd.DataFrame(
        {
            COL_FRAC_DIFF: fd,
            COL_VWAP_DEV: vwap_dev,
            COL_OFI: ofi_series,
            COL_REALIZED_VOL_RATIO: rv_ratio,
            COL_ATR_MOMENTUM: atr_mom,
            COL_ROLLING_SHARPE: r_sharpe,
            COL_VOLUME_ZSCORE: vol_z,
            COL_LABEL: tb_labels,
            COL_RETURN: log_ret,
        },
        index=bars.index,
    )

    # Drop rows with NaN in any feature (burn-in) or where label is NaN
    # (tail rows where holding window extends beyond data)
    before = len(feature_df)
    feature_df = feature_df.dropna(subset=[*FEATURE_COLUMNS, COL_LABEL])
    dropped = before - len(feature_df)

    # For the primary direction classifier: keep only rows where a definitive
    # barrier was hit (label in {0, 1}). Time-exits (label=-1) are excluded.
    direction_mask = feature_df[COL_LABEL].isin([0.0, 1.0])
    feature_df_dir = feature_df[direction_mask].copy()

    # SCAN2-011: meta-labels require the direction model's OOS predictions.
    # Here we use the realized labels as a naive primary signal so the
    # FeatureMatrix.meta field is populated for callers that inspect it;
    # ModelTrainer.train_meta_label() overwrites this with proper OOS predictions.
    ml_series_full = meta_labels(
        feature_df_dir[COL_LABEL].astype(np.int8),
        feature_df_dir[COL_LABEL].astype(np.int8),
    )

    log.info(
        "pipeline.complete",
        n_input=n_input,
        n_output=len(feature_df_dir),
        dropped=dropped,
        label_long=(feature_df_dir[COL_LABEL].isin([1.0])).sum(),
        label_short=(feature_df_dir[COL_LABEL].isin([0.0])).sum(),
    )

    return FeatureMatrix(
        features=feature_df_dir[FEATURE_COLUMNS],
        labels=feature_df_dir[COL_LABEL].astype(np.int8),
        meta=ml_series_full,
        daily_vol=daily_vol.reindex(feature_df_dir.index),
        log_returns=feature_df_dir[COL_RETURN],
        dropped_rows=dropped,
    )


# ---------------------------------------------------------------------------
# Inference-time feature builder — single new bar appended to history
# ---------------------------------------------------------------------------


def build_inference_features(
    history: pd.DataFrame,
    cfg: FeatureSettings | None = None,
    live_ofi: float | None = None,
    feature_matrix: FeatureMatrix | None = None,
    intelligence_metrics: dict[str, float] | None = None,
) -> pd.Series | None:
    """
    Compute feature vector for the most recent bar only.

    Accepts a history DataFrame (must include current bar as last row).
    Returns a pd.Series of FEATURE_COLUMNS (7 base) or FEATURE_COLUMNS +
    INTELLIGENCE_FEATURE_COLUMNS (up to 25 total) when intelligence_metrics
    is supplied and passes NaN validation.

    Parameters
    ----------
    history              : OHLCV DataFrame, last row = most recent closed bar
    cfg                  : FeatureSettings (optional, loaded from config if None)
    live_ofi             : real-time OFI scalar from OrderBookSnapshot (optional).
                           When provided, overrides the OHLCV-derived OFI for the
                           last row only.
    feature_matrix       : pre-computed FeatureMatrix from build_feature_matrix().
                           SCAN2-007: when supplied the full feature computation is
                           skipped — only the last row is extracted and live_ofi
                           is applied, eliminating the duplicate pipeline run.
    intelligence_metrics : flat dict from MultiProviderIntelligenceAggregator.
                           Keys are IntelligenceMetrics field names (no "intelligence_"
                           prefix — the mapping is applied inside _inject_intelligence_features).
                           NaN / missing fields are skipped with a confidence penalty.
                           When None or empty, returns 7-feature base vector (backward-compat).

    Returns
    -------
    pd.Series indexed by FEATURE_COLUMNS [+ finite intelligence cols], or None if
    insufficient base feature data.
    """
    # Fast path — reuse pre-built feature matrix (SCAN2-007)
    if feature_matrix is not None and feature_matrix.features is not None:
        fm = feature_matrix.features
        if len(fm) < 1:
            return None
        vec = fm.iloc[-1][list(FEATURE_COLUMNS)].astype(np.float64)
        if live_ofi is not None:
            vec = vec.copy()
            vec[COL_OFI] = float(live_ofi)
        if vec.isna().any():
            # H-12: promoted to WARNING — DEBUG is invisible in production and
            # causes silent signal blackout that operators won't notice.
            log.warning(
                "pipeline.inference_nan_skip",
                nan_features=vec[vec.isna()].index.tolist(),
                action="signal_skipped — check history length vs feature window config",
            )
            return None
        # Inject intelligence features when available (GAP-015 wiring)
        if intelligence_metrics:
            vec = _inject_intelligence_features(vec, intelligence_metrics)
        return vec
    if cfg is None:
        cfg = effective_feature_settings()

    missing = _REQ_COLS - set(history.columns)
    if missing:
        raise ValueError(f"history DataFrame missing required columns: {missing}")

    n = len(history)
    min_rows = max(
        cfg.vwap_window,
        cfg.ofi_window,
        cfg.realized_vol_window_long,
        cfg.atr_window,
        cfg.sharpe_window,
        cfg.volume_zscore_window,
        64,  # EWMA vol warmup
    )
    if n < min_rows:
        log.debug(
            "pipeline.inference_insufficient",
            n_rows=n,
            min_required=min_rows,
        )
        return None

    close = history["close"].astype(np.float64)
    high = history["high"].astype(np.float64)
    low = history["low"].astype(np.float64)
    volume = history["volume"].astype(np.float64)

    fd_val = fractional_differentiation(
        close, cfg.frac_diff_d, cfg.frac_diff_threshold, _FRAC_DIFF_MAX_WINDOW
    ).iloc[-1]
    vwap_val = vwap_deviation_zscore(high, low, close, volume, cfg.vwap_window).iloc[-1]

    if live_ofi is not None:
        ofi_val = float(live_ofi)
    else:
        ofi_val = order_flow_imbalance(close, volume, cfg.ofi_window).iloc[-1]

    rv_val = realized_vol_ratio(
        close, cfg.realized_vol_window_short, cfg.realized_vol_window_long
    ).iloc[-1]
    atr_val = atr_momentum(high, low, close, cfg.atr_window).iloc[-1]
    sharpe_val = rolling_sharpe(close, cfg.sharpe_window).iloc[-1]
    volz_val = volume_zscore(volume, cfg.volume_zscore_window).iloc[-1]

    vec = pd.Series(
        {
            COL_FRAC_DIFF: fd_val,
            COL_VWAP_DEV: vwap_val,
            COL_OFI: float(ofi_val),
            COL_REALIZED_VOL_RATIO: rv_val,
            COL_ATR_MOMENTUM: atr_val,
            COL_ROLLING_SHARPE: sharpe_val,
            COL_VOLUME_ZSCORE: volz_val,
        },
        dtype=np.float64,
    )

    if vec.isna().any():
        # H-12: promoted to WARNING — DEBUG is invisible in production and
        # causes silent signal blackout that operators won't notice.
        log.warning(
            "pipeline.inference_nan_skip",
            nan_features=vec[vec.isna()].index.tolist(),
            action="signal_skipped — check history length vs feature window config",
        )
        return None

    # Inject intelligence features when available (GAP-015 wiring)
    if intelligence_metrics:
        vec = _inject_intelligence_features(vec, intelligence_metrics)

    return vec


# ---------------------------------------------------------------------------
# Intelligence feature injection helper (GAP-015)
# ---------------------------------------------------------------------------


def _inject_intelligence_features(
    vec: pd.Series,
    intelligence_metrics: dict[str, float],
) -> pd.Series:
    """
    Append intelligence feature columns to a base feature vector.

    Mapping: provider dict key (e.g. "exchange_stress_score") →
             column name (e.g. "intelligence_exchange_stress_score").

    Only finite (non-NaN, non-Inf) values are injected; fields that are
    NaN in the provider output are silently dropped so the downstream
    model receives only real values (avoids silent fabrication).

    The returned Series extends the input without modifying it.
    Confidence is included as "intelligence_confidence" when present.

    Args:
        vec:                  Base 7-feature pd.Series.
        intelligence_metrics: Flat dict from MultiProviderIntelligenceAggregator.

    Returns:
        Extended pd.Series with up to 18 additional intelligence columns.
    """
    from src.features.intelligence_features import (
        COL_INTELLIGENCE_CONFIDENCE,
        INTELLIGENCE_FEATURE_COLUMNS,
    )

    # Build prefix mapping: "exchange_stress_score" → "intelligence_exchange_stress_score"
    # INTELLIGENCE_FEATURE_COLUMNS uses the "intelligence_" prefix; strip it for lookup.
    extras: dict[str, float] = {}
    for col in INTELLIGENCE_FEATURE_COLUMNS:
        raw_key = col.removeprefix("intelligence_")
        val = intelligence_metrics.get(raw_key)
        if val is None:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        import math as _math

        if _math.isfinite(fval):
            extras[col] = fval

    # Always include confidence when available and finite
    conf = intelligence_metrics.get("confidence")
    if conf is not None:
        try:
            cval = float(conf)
            import math as _math

            if _math.isfinite(cval):
                extras[COL_INTELLIGENCE_CONFIDENCE] = cval
        except (TypeError, ValueError):
            pass

    if not extras:
        return vec

    import numpy as _np
    import pandas as _pd

    extras_series = _pd.Series(extras, dtype=_np.float64)
    result = _pd.concat([vec, extras_series])

    log.debug(
        "pipeline.intelligence_features_injected",
        n_injected=len(extras),
        confidence=extras.get(COL_INTELLIGENCE_CONFIDENCE),
    )
    return result
