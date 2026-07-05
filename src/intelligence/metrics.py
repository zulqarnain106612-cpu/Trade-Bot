"""
Intelligence metrics computation layer.

Transforms raw provider data into trading-ready features:
  - Normalization (z-scores, percentiles)
  - Time-series aggregation (rolling windows)
  - Risk scoring (composite metrics)
  - Signal interpretation (bullish/bearish thresholds)

Authority: López de Prado (2018) AFML, Cont et al. flow analysis
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog


log = structlog.get_logger(__name__)


@dataclass
class IntelligenceMetrics:
    """
    Computed intelligence metrics (derived from raw provider data).

    Ready for feature engineering consumption.
    """

    # Exchange flow metrics (6)
    exchange_netflow_7d_zscore: float          # Z-score of netflow vs 30d MA
    whale_buy_sell_ratio: float                # Large txn buy/sell volume ratio
    exchange_reserve_ratio: float              # Coins on exchange / total supply
    miner_netflow_signal: float                # Miner selling pressure (-1 to +1)
    staking_unlock_risk: float                 # Upcoming unlock events (0-1 score)
    entity_exchange_imbalance: float           # Whale concentration risk (0-1)

    # Leverage metrics (4)
    binance_funding_rate_pct: float            # Current funding rate %
    liquidation_pressure_24h_zscore: float     # Z-score of 24h liquidations
    futures_oi_change_pct: float               # Open interest % change (24h)
    liquidation_cascade_risk_usd: float        # Est. cascade liquidations ($)

    # Macro regime (3)
    btc_dominance_regime: float                # BTC.D zscore vs 60d MA
    stablecoin_reserve_ratio: float            # USDC+USDT / total crypto market cap
    network_activity_score: float              # On-chain activity momentum (-1 to +1)

    # Exchange health (2)
    exchange_stress_score: float               # Composite health (0-1, 1=max stress)
    cross_exchange_basis_spread_bps: float     # Basis between Binance/OKX (bps)

    # Metadata
    timestamp: int                             # Unix seconds
    confidence: float                          # Data quality (0-1, missing data = low)

    def to_dict(self) -> dict[str, float]:
        """Export as flat dict for feature pipeline."""
        return {
            "exchange_netflow_7d_zscore": self.exchange_netflow_7d_zscore,
            "whale_buy_sell_ratio": self.whale_buy_sell_ratio,
            "exchange_reserve_ratio": self.exchange_reserve_ratio,
            "miner_netflow_signal": self.miner_netflow_signal,
            "staking_unlock_risk": self.staking_unlock_risk,
            "entity_exchange_imbalance": self.entity_exchange_imbalance,
            "binance_funding_rate_pct": self.binance_funding_rate_pct,
            "liquidation_pressure_24h_zscore": self.liquidation_pressure_24h_zscore,
            "futures_oi_change_pct": self.futures_oi_change_pct,
            "liquidation_cascade_risk_usd": self.liquidation_cascade_risk_usd,
            "btc_dominance_regime": self.btc_dominance_regime,
            "stablecoin_reserve_ratio": self.stablecoin_reserve_ratio,
            "network_activity_score": self.network_activity_score,
            "exchange_stress_score": self.exchange_stress_score,
            "cross_exchange_basis_spread_bps": self.cross_exchange_basis_spread_bps,
        }


class IntelligenceAnalyzer:
    """
    Aggregator that transforms raw provider data into IntelligenceMetrics.

    Handles:
      - Z-score normalization (vs rolling historical baselines)
      - Composite scoring (weighted aggregation)
      - Confidence scoring (missing data penalty)
    """

    # GAP-015: of the 15 IntelligenceMetrics fields, only these are backed
    # by a real, live data path today: exchange_netflow_7d_zscore,
    # whale_buy_sell_ratio, binance_funding_rate_pct (all from the free
    # Binance provider), and exchange_stress_score (a composite derived
    # from the first and third, not an independent fetch). The remaining
    # 11 are NaN until a real free data source is wired in (see
    # DECISION_LOG.md). Update these two constants if that changes.
    _REAL_METRIC_COUNT = 4
    _TOTAL_METRIC_COUNT = 15

    def __init__(
        self,
        historical_data: pd.DataFrame | None = None,
        rolling_window_days: int = 30,
    ):
        """Initialize analyzer.

        Args:
            historical_data: DataFrame with historical metrics for z-score baseline
            rolling_window_days: Window for computing MA/std (default 30d)
        """
        self.historical_data = historical_data or pd.DataFrame()
        self.rolling_window = rolling_window_days
        self._missing_data_penalty = 0.1  # confidence -= 0.1 per missing metric

    def compute_metrics(
        self,
        exchange_netflow: dict,           # From Glassnode
        whale_activity: dict,             # From Glassnode
        funding_rate: dict,               # From CryptoQuant
        macro_regime: dict | None = None,  # From macro data source
    ) -> IntelligenceMetrics:
        """
        Compute full intelligence metrics from provider data.

        Args:
            exchange_netflow: {"netflow": float, "inflow": float, ...}
            whale_activity: {"buy_volume": float, "sell_volume": float, ...}
            funding_rate: {"rate_pct": float, "excessive": bool, ...}
            macro_regime: Optional {"btc_dominance": float, ...}

        Returns:
            IntelligenceMetrics ready for feature pipeline
        """
        confidence = 1.0
        missing_metrics = 0

        # Exchange flow: netflow z-score (vs 30d MA)
        try:
            netflow_zscore = self._compute_zscore(
                exchange_netflow.get("netflow", 0),
                metric_name="exchange_netflow",
            )
        except Exception as e:
            log.warning("exchange_netflow_zscore_compute_failed", error=str(e))
            netflow_zscore = 0.0
            missing_metrics += 1
            confidence -= self._missing_data_penalty

        # Whale sentiment: buy/sell ratio
        try:
            whale_ratio = whale_activity.get("ratio", 1.0)
            # Clamp and normalize: ratio > 3 = bullish, < 0.33 = bearish
            np.clip(np.log(whale_ratio + 0.01) / np.log(3), -1, 1)
        except Exception as e:
            log.warning("whale_ratio_compute_failed", error=str(e))
            missing_metrics += 1
            confidence -= self._missing_data_penalty

        # Funding rate excessive (binary feature, converted to -1/0/+1)
        try:
            rate_pct = funding_rate.get("rate_pct", 0)
            funding_signal = (
                1.0 if rate_pct > 0.1 else (-1.0 if rate_pct < -0.05 else 0.0)
            )
        except Exception as e:
            log.warning("funding_rate_signal_failed", error=str(e))
            funding_signal = 0.0
            missing_metrics += 1
            confidence -= self._missing_data_penalty

        # GAP-015 fix: previously, 11 of 15 metrics were hardcoded plausible-
        # looking constants (e.g. exchange_reserve_ratio=0.35) marked only by
        # a code comment, and `confidence` was never penalized for them --
        # so this could return confidence~=1.0 while 11/15 fields were fake.
        # That's a fabricated-completion-state bug: a downstream consumer
        # reading `confidence` alone had no way to know most of the payload
        # was a placeholder. Fixed: unimplemented fields are now NaN (so
        # they fail loud in any consumer that isn't NaN-aware, matching the
        # existing NaN-handling convention in build_inference_features), and
        # confidence is capped by the real fraction of implemented metrics
        # BEFORE the existing per-call exception penalty is applied.
        _real_fraction = self._REAL_METRIC_COUNT / self._TOTAL_METRIC_COUNT
        confidence = min(confidence, _real_fraction)

        # Clamp confidence to [0, 1]
        confidence = np.clip(confidence, 0.0, 1.0)

        _nan = float("nan")

        # Create metrics object. Only 4/15 fields are real/derived from live
        # provider data today (see self._REAL_METRIC_COUNT); the rest are
        # NaN pending GAP-015 data-source integration (see DECISION_LOG.md
        # "Intelligence feature wiring -- blocked on API provisioning").
        return IntelligenceMetrics(
            # Exchange flow (implemented: 2/6, rest NaN pending real source)
            exchange_netflow_7d_zscore=netflow_zscore,
            whale_buy_sell_ratio=whale_ratio,
            exchange_reserve_ratio=_nan,      # NEEDS: Glassnode or GraphSense
            miner_netflow_signal=_nan,        # NEEDS: Glassnode or GraphSense
            staking_unlock_risk=_nan,         # NEEDS: no free source found (GAP-015)
            entity_exchange_imbalance=_nan,   # NEEDS: Glassnode or GraphSense
            # Leverage (implemented: 1/4, rest NaN pending real source)
            binance_funding_rate_pct=rate_pct,
            liquidation_pressure_24h_zscore=_nan,  # NEEDS: no free source found (GAP-015)
            futures_oi_change_pct=_nan,            # NEEDS: CryptoQuant or Binance OI hist (30d cap)
            liquidation_cascade_risk_usd=_nan,     # NEEDS: no free source found (GAP-015)
            # Macro regime (0/3, NaN pending real source)
            btc_dominance_regime=_nan,       # NEEDS: CoinGecko /global (free, unwired)
            stablecoin_reserve_ratio=_nan,   # NEEDS: blockchain data (free, unwired)
            network_activity_score=_nan,     # NEEDS: blockchain.info charts (free, unwired)
            # Exchange health (1/2: stress score is derived+real, basis spread NaN)
            exchange_stress_score=self._compute_exchange_stress(
                netflow_zscore, funding_signal
            ),
            cross_exchange_basis_spread_bps=_nan,  # NEEDS: Binance spot+futures klines (free, unwired)
            # Metadata
            timestamp=int(datetime.now(UTC).timestamp()),
            confidence=confidence,
        )

    def _compute_zscore(
        self,
        value: float,
        metric_name: str,
        window_days: int | None = None,
    ) -> float:
        """
        Compute z-score vs historical rolling window.

        Args:
            value: Current metric value
            metric_name: Column name in historical_data
            window_days: Override default window (None = use self.rolling_window)

        Returns:
            Z-score: (value - mean) / std
        """
        if self.historical_data.empty or metric_name not in self.historical_data:
            # No history, return neutral
            return 0.0

        window = window_days or self.rolling_window
        recent = self.historical_data[metric_name].tail(window)

        if len(recent) < 2:
            return 0.0

        mean = recent.mean()
        std = recent.std()

        if std == 0:
            return 0.0

        zscore = (value - mean) / std
        # Clamp to [-5, 5] to avoid extreme outliers
        return float(np.clip(zscore, -5, 5))

    def _compute_exchange_stress(
        self,
        netflow_zscore: float,
        funding_signal: float,
    ) -> float:
        """
        Composite exchange stress score (0=healthy, 1=extreme stress).

        Combines:
          - Extreme netflow (zscore > 2 = sellers fleeing)
          - Excessive funding rate (leverage bubble)
          - (Future: Exchange reserve ratio, basis spread)
        """
        stress = 0.0

        # Netflow stress: sellers leaving rapidly
        if netflow_zscore < -2.0:
            stress += min(abs(netflow_zscore) / 5.0, 0.5)  # Cap at 0.5

        # Funding stress: excess leverage
        if funding_signal > 0.5:
            stress += 0.3

        return min(stress, 1.0)  # Cap at 1.0
