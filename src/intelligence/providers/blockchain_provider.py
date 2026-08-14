"""
Blockchain.info free public REST provider for network activity metrics.

Covers on-chain network health signals that no CEX can supply:
  ✅ network_activity_score — composite of confirmed txns + hash rate momentum

Data source: blockchain.info/charts (free, no auth, JSON format).
  - https://blockchain.info/stats?format=json  (24h summary stats)

Rate limits: ~1 req/s sustained. Cache TTL 900s keeps well under limit.

Authority:
  blockchain.info stats API: https://www.blockchain.com/explorer/api/blockchain_api
  Woo et al. (2021) "Predicting Bitcoin Returns from On-Chain Metrics" — NVT,
    hash rate, and tx throughput as leading macro-regime signals.
  Nakamoto (2008) Bitcoin whitepaper — hash rate as network security proxy.
"""

from __future__ import annotations

import time
from typing import Any, Final

import aiohttp
import structlog

from src.intelligence.providers.base import ExchangeIntelligenceProvider


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BLOCKCHAIN_STATS_URL: Final[str] = "https://blockchain.info/stats?format=json"
_CACHE_TTL_S: Final[int] = 900  # 15-min cache; on-chain data low-frequency
_CONFIDENCE_PENALTY: Final[float] = 0.05
# Rolling windows for z-score momentum computation (stored across calls)
_HASHRATE_WINDOW: Final[int] = 48  # 48 x 15-min = 12h for hash rate momentum
_TX_WINDOW: Final[int] = 48


class BlockchainIntelligenceProvider(ExchangeIntelligenceProvider):
    """
    Fetches BTC network activity metrics from blockchain.info/stats.

    Provides:
      - network_activity_score: composite of tx count momentum +
        hash rate momentum, normalized to [-1, +1].
        Positive = network accelerating (bullish on-chain signal).
        Negative = network decelerating (bearish on-chain signal).

    All other IntelligenceMetrics fields pass through as neutral.
    exchange_id is "blockchain_info" to distinguish it in aggregator logs.
    """

    def __init__(self, cache_ttl_s: int = _CACHE_TTL_S) -> None:
        self._cache_ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, Any]] = {}
        # Rolling history for momentum computation
        self._hashrate_history: list[float] = []
        self._tx_history: list[float] = []
        self._log = log.bind(component="blockchain_intelligence")

    @property
    def exchange_id(self) -> str:
        return "blockchain_info"

    async def initialize(self) -> None:
        self._log.info("blockchain_intelligence.initialized")

    async def close(self) -> None:
        """Nothing to close."""

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch network activity from blockchain.info/stats.

        network_activity_score = weighted mean of:
          - hash rate momentum z-score (weight 0.5): security/miner confidence
          - tx count momentum z-score  (weight 0.5): economic activity

        Both normalized via rolling z-score, then averaged, then clamped to [-1, 1].

        Never raises.
        """
        ts = int(time.time())
        confidence = 1.0
        network_score: float = 0.0

        try:
            stats = await self._fetch_stats()
            if stats:
                # Hash rate (TH/s) — proxy for miner confidence
                hash_rate = float(stats.get("hash_rate", 0.0) or 0.0)
                # Confirmed tx count per day
                n_txs = float(stats.get("n_tx", 0.0) or 0.0)

                # Accumulate rolling histories
                if hash_rate > 0:
                    self._hashrate_history.append(hash_rate)
                if n_txs > 0:
                    self._tx_history.append(n_txs)

                # Trim to window
                self._hashrate_history = self._hashrate_history[-_HASHRATE_WINDOW:]
                self._tx_history = self._tx_history[-_TX_WINDOW:]

                hr_zscore = self._zscore(self._hashrate_history)
                tx_zscore = self._zscore(self._tx_history)

                # Weighted composite — equally weighted; clamp to [-1, 1]
                raw = 0.5 * hr_zscore + 0.5 * tx_zscore
                network_score = max(-1.0, min(1.0, raw))
            else:
                self._log.warning("blockchain_intelligence.empty_stats_response")
                confidence -= _CONFIDENCE_PENALTY
        except Exception as exc:
            self._log.warning("blockchain_intelligence.fetch_failed", error=str(exc), exc_info=True)
            confidence -= _CONFIDENCE_PENALTY

        confidence = max(0.0, min(1.0, confidence))

        return {
            # OWNED by this provider
            "network_activity_score": network_score,
            # NOT this provider's domain — neutral passthrough
            "binance_funding_rate_pct": 0.0,
            "futures_oi_change_pct": 0.0,
            "cross_exchange_basis_spread_bps": 0.0,
            "whale_buy_sell_ratio": 1.0,
            "liquidation_pressure_24h_zscore": 0.0,
            "liquidation_cascade_risk_usd": 0.0,
            "exchange_stress_score": 0.0,
            "exchange_netflow_7d_zscore": 0.0,
            "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0,
            "staking_unlock_risk": 0.0,
            "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0,
            "stablecoin_reserve_ratio": 0.5,
            # Metadata
            "confidence": confidence,
            "timestamp": ts,
        }

    async def _fetch_stats(self) -> dict:
        """Fetch blockchain.info/stats with in-memory cache."""
        cache_key = "stats"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        timeout = aiohttp.ClientTimeout(total=8.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _BLOCKCHAIN_STATS_URL,
                headers={"Accept": "application/json"},
            ) as resp:
                resp.raise_for_status()
                data: dict = await resp.json()
                self._set_cache(cache_key, data)
                return data

    @staticmethod
    def _zscore(history: list[float]) -> float:
        """Rolling z-score of last value in history. Returns 0.0 if window < 4."""
        if len(history) < 4:
            return 0.0
        import statistics as _stats

        mu = _stats.mean(history)
        sigma = _stats.stdev(history)
        if sigma < 1e-12:
            return 0.0
        return max(-3.0, min(3.0, (history[-1] - mu) / sigma))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: BlockchainIntelligenceProvider | None = None


def get_blockchain_intelligence_provider() -> BlockchainIntelligenceProvider:
    """Return the module-level BlockchainIntelligenceProvider singleton."""
    global _provider
    if _provider is None:
        _provider = BlockchainIntelligenceProvider()
    return _provider
