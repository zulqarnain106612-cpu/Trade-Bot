"""
Producer for :class:`~src.intelligence.macro_regime.MacroIndicators`.

``macro_regime.classify_macro_regime`` and ``risk.macro_exposure_budget``
were both written against a ``MacroIndicators`` value that nothing in the
tree ever built, so the whole v7 macro overlay was inert. This module is
the missing producer: it derives the three indicators from the
intelligence-feature history already persisted by
``Storage.store_intelligence_features``, so no new (paid) data source is
introduced.

Column mapping, all from ``Storage.fetch_intelligence_features``:

  funding_rate_zscore_avg
      z-score of ``intelligence_binance_funding_rate_pct`` — the latest
      funding print measured against its own rolling window. Funding is a
      free Binance endpoint, unlike the Glassnode/CryptoQuant series.
  stablecoin_supply_growth_pct
      window pct-change of ``intelligence_stablecoin_reserve_ratio``,
      used as a proxy for stablecoin capital entering the exchange system.
  net_exchange_inflow_zscore
      ``intelligence_exchange_netflow_7d_zscore`` as published — already a
      z-score, positive = net inflow to exchanges.

Returns ``None`` rather than a neutral value whenever the window is too
short or a column is entirely missing. A neutral ``MacroIndicators`` would
still map to a ~0.62 exposure scalar and silently shrink every position on
absent data; ``None`` lets the caller skip the overlay entirely, which is
the correct no-information behaviour.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from src.intelligence.macro_regime import MacroIndicators

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd


_FUNDING_COL = "intelligence_binance_funding_rate_pct"
_STABLECOIN_COL = "intelligence_stablecoin_reserve_ratio"
_NETFLOW_COL = "intelligence_exchange_netflow_7d_zscore"

# Below this many observations a z-score is noise, not signal.
MIN_OBSERVATIONS: int = 8


def build_macro_indicators(
    features: pd.DataFrame,
    *,
    min_observations: int = MIN_OBSERVATIONS,
) -> MacroIndicators | None:
    """
    Build ``MacroIndicators`` from an intelligence-feature frame.

    Parameters
    ----------
    features
        Frame as returned by ``Storage.fetch_intelligence_features``:
        indexed by ``bar_ts`` ascending, one column per intelligence
        feature. May be empty, and any column may be fully NULL.
    min_observations
        Minimum non-NULL rows required per column before that column is
        trusted. Columns that fall short contribute 0.0 (neutral) rather
        than blocking the whole overlay, but if *every* column falls short
        the function returns ``None``.

    Returns
    -------
    MacroIndicators | None
        ``None`` when no column carried enough data to say anything.
    """
    if features is None or features.empty:
        return None

    funding, funding_ok = _zscore_of_latest(features, _FUNDING_COL, min_observations)
    stable, stable_ok = _window_growth_pct(features, _STABLECOIN_COL, min_observations)
    netflow, netflow_ok = _latest_value(features, _NETFLOW_COL, min_observations)

    if not (funding_ok or stable_ok or netflow_ok):
        return None

    return MacroIndicators(
        funding_rate_zscore_avg=funding,
        stablecoin_supply_growth_pct=stable,
        net_exchange_inflow_zscore=netflow,
    )


def _series(features: pd.DataFrame, column: str, min_observations: int) -> pd.Series | None:
    """Non-NULL, finite values of *column*, or ``None`` if too few."""
    if column not in features.columns:
        return None
    series = features[column].dropna()
    # Storage columns are REAL/DOUBLE; a stored inf would poison every
    # downstream statistic, so drop non-finite values with the NULLs.
    series = series[[math.isfinite(float(v)) for v in series]]
    if len(series) < min_observations:
        return None
    return series


def _zscore_of_latest(
    features: pd.DataFrame, column: str, min_observations: int
) -> tuple[float, bool]:
    """Latest observation expressed as a z-score of its own window."""
    series = _series(features, column, min_observations)
    if series is None:
        return 0.0, False
    std = float(series.std(ddof=1))
    # A flat window carries no dispersion information. The threshold is
    # relative, not `std <= 0.0`: pandas computes std by a numerically-stable
    # route that leaves ~1e-18 residue on a genuinely constant series, and
    # dividing a similarly tiny numerator by it manufactures a confident
    # z-score out of pure floating-point noise (observed: z=0.96 from twelve
    # identical 0.01 funding prints).
    scale = float(series.abs().max()) or 1.0
    if not math.isfinite(std) or std <= scale * 1e-9:
        return 0.0, False
    return (float(series.iloc[-1]) - float(series.mean())) / std, True


def _window_growth_pct(
    features: pd.DataFrame, column: str, min_observations: int
) -> tuple[float, bool]:
    """Pct change between the first and last observation of the window."""
    series = _series(features, column, min_observations)
    if series is None:
        return 0.0, False
    first = float(series.iloc[0])
    if first == 0.0:
        return 0.0, False
    growth = (float(series.iloc[-1]) - first) / abs(first) * 100.0
    if not math.isfinite(growth):
        return 0.0, False
    return growth, True


def _latest_value(features: pd.DataFrame, col: str, min_observations: int) -> tuple[float, bool]:
    """Latest observation of an already-normalized column."""
    series = _series(features, col, min_observations)
    if series is None:
        return 0.0, False
    return float(series.iloc[-1]), True
