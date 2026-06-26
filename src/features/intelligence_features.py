"""
Intelligence-augmented features.

Extends core feature pipeline (9 features) with crypto intelligence (15 features).

Current features (9):
  1-3. Fractional differentiation, VWAP deviation, Order flow imbalance
  4-6. Realized volatility, ATR momentum, Rolling Sharpe
  7-9. Volume z-score, Triple-barrier labels, Meta-labels

New intelligence features (15):
  10-15. Exchange flow (6): netflow zscore, whale ratio, reserve ratio, ...
  16-19. Leverage (4): funding rate, liquidation pressure, OI change, cascade risk
  20-22. Macro regime (3): BTC dominance, stablecoin ratio, network activity
  23-24. Exchange health (2): stress score, basis spread

Author: Trade Bot Intelligence Layer
Authority: Glassnode/CryptoQuant APIs, AFML architecture
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import structlog

from src.intelligence.metrics import IntelligenceMetrics

log = structlog.get_logger(__name__)

# ============================================================================
# Feature column names (export for consistency with pipeline.py)
# ============================================================================

# Exchange flow features
COL_EXCHANGE_NETFLOW_7D_ZSCORE = "intelligence_exchange_netflow_7d_zscore"
COL_WHALE_BUY_SELL_RATIO = "intelligence_whale_buy_sell_ratio"
COL_EXCHANGE_RESERVE_RATIO = "intelligence_exchange_reserve_ratio"
COL_MINER_NETFLOW_SIGNAL = "intelligence_miner_netflow_signal"
COL_STAKING_UNLOCK_RISK = "intelligence_staking_unlock_risk"
COL_ENTITY_EXCHANGE_IMBALANCE = "intelligence_entity_exchange_imbalance"

# Leverage features
COL_BINANCE_FUNDING_RATE_PCT = "intelligence_binance_funding_rate_pct"
COL_LIQUIDATION_PRESSURE_24H_ZSCORE = "intelligence_liquidation_pressure_24h_zscore"
COL_FUTURES_OI_CHANGE_PCT = "intelligence_futures_oi_change_pct"
COL_LIQUIDATION_CASCADE_RISK_USD = "intelligence_liquidation_cascade_risk_usd"

# Macro regime features
COL_BTC_DOMINANCE_REGIME = "intelligence_btc_dominance_regime"
COL_STABLECOIN_RESERVE_RATIO = "intelligence_stablecoin_reserve_ratio"
COL_NETWORK_ACTIVITY_SCORE = "intelligence_network_activity_score"

# Exchange health features
COL_EXCHANGE_STRESS_SCORE = "intelligence_exchange_stress_score"
COL_CROSS_EXCHANGE_BASIS_SPREAD_BPS = "intelligence_cross_exchange_basis_spread_bps"

# Metadata
COL_INTELLIGENCE_CONFIDENCE = "intelligence_confidence"

INTELLIGENCE_FEATURE_COLUMNS = [
    COL_EXCHANGE_NETFLOW_7D_ZSCORE,
    COL_WHALE_BUY_SELL_RATIO,
    COL_EXCHANGE_RESERVE_RATIO,
    COL_MINER_NETFLOW_SIGNAL,
    COL_STAKING_UNLOCK_RISK,
    COL_ENTITY_EXCHANGE_IMBALANCE,
    COL_BINANCE_FUNDING_RATE_PCT,
    COL_LIQUIDATION_PRESSURE_24H_ZSCORE,
    COL_FUTURES_OI_CHANGE_PCT,
    COL_LIQUIDATION_CASCADE_RISK_USD,
    COL_BTC_DOMINANCE_REGIME,
    COL_STABLECOIN_RESERVE_RATIO,
    COL_NETWORK_ACTIVITY_SCORE,
    COL_EXCHANGE_STRESS_SCORE,
    COL_CROSS_EXCHANGE_BASIS_SPREAD_BPS,
]


def add_intelligence_features(
    df: pd.DataFrame,
    intelligence_metrics: IntelligenceMetrics,
) -> pd.DataFrame:
    """
    Add intelligence metrics columns to OHLCV feature DataFrame.

    Args:
        df: Feature DataFrame (result of build_feature_matrix())
        intelligence_metrics: Computed metrics from IntelligenceAnalyzer

    Returns:
        DataFrame with 15 additional intelligence columns
    """
    if df.empty:
        log.warning("add_intelligence_features_empty_input")
        return df

    # Create a copy to avoid mutation
    result = df.copy()

    # Add all 15 intelligence features
    # (Using last row timestamp, or broadcast if needed)
    result[COL_EXCHANGE_NETFLOW_7D_ZSCORE] = intelligence_metrics.exchange_netflow_7d_zscore
    result[COL_WHALE_BUY_SELL_RATIO] = intelligence_metrics.whale_buy_sell_ratio
    result[COL_EXCHANGE_RESERVE_RATIO] = intelligence_metrics.exchange_reserve_ratio
    result[COL_MINER_NETFLOW_SIGNAL] = intelligence_metrics.miner_netflow_signal
    result[COL_STAKING_UNLOCK_RISK] = intelligence_metrics.staking_unlock_risk
    result[COL_ENTITY_EXCHANGE_IMBALANCE] = intelligence_metrics.entity_exchange_imbalance
    result[COL_BINANCE_FUNDING_RATE_PCT] = intelligence_metrics.binance_funding_rate_pct
    result[COL_LIQUIDATION_PRESSURE_24H_ZSCORE] = intelligence_metrics.liquidation_pressure_24h_zscore
    result[COL_FUTURES_OI_CHANGE_PCT] = intelligence_metrics.futures_oi_change_pct
    result[COL_LIQUIDATION_CASCADE_RISK_USD] = intelligence_metrics.liquidation_cascade_risk_usd
    result[COL_BTC_DOMINANCE_REGIME] = intelligence_metrics.btc_dominance_regime
    result[COL_STABLECOIN_RESERVE_RATIO] = intelligence_metrics.stablecoin_reserve_ratio
    result[COL_NETWORK_ACTIVITY_SCORE] = intelligence_metrics.network_activity_score
    result[COL_EXCHANGE_STRESS_SCORE] = intelligence_metrics.exchange_stress_score
    result[COL_CROSS_EXCHANGE_BASIS_SPREAD_BPS] = intelligence_metrics.cross_exchange_basis_spread_bps
    result[COL_INTELLIGENCE_CONFIDENCE] = intelligence_metrics.confidence

    log.info(
        "intelligence_features_added",
        num_rows=len(result),
        num_cols_added=15,
        confidence=intelligence_metrics.confidence,
    )

    return result


@dataclass
class IntelligenceFeatureMatrix:
    """
    Result of intelligence-augmented feature engineering.

    Combines original 9 features + 15 intelligence features = 24 total.
    """

    X: pd.DataFrame                      # Shape (n_samples, 24)
    y_direction: Optional[pd.Series] = None  # Direction labels (if training)
    y_meta: Optional[pd.Series] = None       # Meta-labels (if training)
    intelligence_metrics: Optional[IntelligenceMetrics] = None

    @property
    def feature_count(self) -> int:
        """Total number of features (should be 24)."""
        return self.X.shape[1] if self.X is not None else 0

    @property
    def sample_count(self) -> int:
        """Number of samples."""
        return len(self.X) if self.X is not None else 0
