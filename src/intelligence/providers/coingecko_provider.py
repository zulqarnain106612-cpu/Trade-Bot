"""
CoinGecko free public REST provider for macro intelligence metrics.

Covers cross-market metrics that no single exchange can supply:
  ✅ btc_dominance_regime        — BTC.D z-score vs 60d MA (/global)
  ✅ stablecoin_reserve_ratio    — (USDC+USDT mktcap) / total crypto mktcap

Rate limits (free tier, no key required):
  - 10-30 req/min sustained (CoinGecko demo tier, shared IP).
  - Cache TTL 900s (15 min) keeps well under limit even at 3 engine ticks/min.

Authority:
  CoinGecko REST API v3: https://www.coingecko.com/en/api/documentation
  BTC dominance as macro regime signal: Cointelegraph research (2021);
    López de Prado (2018) AFML Ch.15 — regime features.
"""

from __future__ import annotations

import time
from typing import Any, Final

import aiohttp
import structlog

from src.intelligence.providers.base import ExchangeIntelligenceProvider


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_COINGECKO_GLOBAL_URL: Final[str] = "https://api.coingecko.com/api/v3/global"
_CACHE_TTL_S: Final[int] = 900  # 15-min cache; data updates ~1/min but stale is fine
_STABLECOIN_COINS: Final[tuple[str, ...]] = ("tether", "usd-coin", "dai", "frax", "usdd")
# BTC dominance z-score window (approximate: stored as last N fetches, 15-min cadence)
_BTC_DOM_WINDOW: Final[int] = 96  # 96 x 15min = 24h rolling window for z-score
_CONFIDENCE_PENALTY: Final[float] = 0.05
_MISSING_PAID_FIELDS: Final[int] = 0  # all fields here are free


class CoinGeckoIntelligenceProvider(ExchangeIntelligenceProvider):
    """
    Fetches macro intelligence metrics from CoinGecko /global endpoint.

    Provides two fields that no exchange-specific provider can supply:
      - btc_dominance_regime    : BTC.D z-score vs rolling 24h window
      - stablecoin_reserve_ratio: USDC+USDT market cap / total crypto market cap

    All other IntelligenceMetrics fields are set to neutral and clearly
    marked as not-this-provider's-responsibility.

    This provider is exchange-agnostic (cross-market data).  The
    exchange_id is "coingecko" to distinguish it in aggregator logs.
    """

    def __init__(self, cache_ttl_s: int = _CACHE_TTL_S) -> None:
        self._cache_ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, Any]] = {}
        # Rolling BTC dominance history for z-score computation
        self._btc_dom_history: list[float] = []
        self._log = log.bind(component="coingecko_intelligence")

    @property
    def exchange_id(self) -> str:
        return "coingecko"

    async def initialize(self) -> None:
        """No setup needed — HTTP requests are made on first fetch_metrics() call."""
        self._log.info("coingecko_intelligence.initialized")

    async def close(self) -> None:
        """Nothing to close — aiohttp sessions are per-request."""

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch macro metrics from CoinGecko /global.

        Returns a partial metrics dict. Fields not covered by CoinGecko
        are set to their neutral value; confidence covers only the
        two fields this provider owns.

        Never raises.
        """
        ts = int(time.time())
        confidence = 1.0

        btc_dominance_zscore: float = 0.0
        stablecoin_ratio: float = 0.5

        try:
            global_data = await self._fetch_global()
            if global_data:
                mkt_cap_pct: dict = global_data.get("market_cap_percentage", {})

                # BTC dominance z-score
                btc_dom_pct = float(mkt_cap_pct.get("btc", 0.0))
                self._btc_dom_history.append(btc_dom_pct)
                if len(self._btc_dom_history) > _BTC_DOM_WINDOW:
                    self._btc_dom_history = self._btc_dom_history[-_BTC_DOM_WINDOW:]
                if len(self._btc_dom_history) >= 4:
                    import statistics as _stats

                    mu = _stats.mean(self._btc_dom_history)
                    sigma = _stats.stdev(self._btc_dom_history)
                    btc_dominance_zscore = (btc_dom_pct - mu) / sigma if sigma > 1e-9 else 0.0
                    btc_dominance_zscore = max(-3.0, min(3.0, btc_dominance_zscore))

                # Stablecoin reserve ratio
                total_mktcap = float(global_data.get("total_market_cap", {}).get("usd", 0.0) or 0.0)
                if total_mktcap > 0:
                    stable_pct = sum(
                        float(mkt_cap_pct.get(coin_id, 0.0))
                        for coin_id in ("usdt", "usdc", "dai", "frax")
                    )
                    stablecoin_ratio = max(0.0, min(1.0, stable_pct / 100.0))
            else:
                self._log.warning("coingecko_intelligence.empty_global_response")
                confidence -= _CONFIDENCE_PENALTY * 2  # both fields failed
        except Exception as exc:
            self._log.warning("coingecko_intelligence.fetch_failed", error=str(exc), exc_info=True)
            confidence -= _CONFIDENCE_PENALTY * 2

        confidence = max(0.0, min(1.0, confidence))

        return {
            # OWNED by this provider (free CoinGecko)
            "btc_dominance_regime": btc_dominance_zscore,
            "stablecoin_reserve_ratio": stablecoin_ratio,
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
            "network_activity_score": 0.0,
            # OCI-012: on-chain fields — neutral defaults for macro provider
            "defi_tvl_7d_change_pct": 0.0,
            "mvrv_z_score": 0.0,
            "sopr": 0.0,
            # Metadata
            "confidence": confidence,
            "timestamp": ts,
        }

    async def _fetch_global(self) -> dict:
        """Fetch /global with in-memory cache."""
        cache_key = "global"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        timeout = aiohttp.ClientTimeout(total=8.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _COINGECKO_GLOBAL_URL,
                headers={"Accept": "application/json"},
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
                data: dict = body.get("data", {})
                self._set_cache(cache_key, data)
                return data


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: CoinGeckoIntelligenceProvider | None = None


def get_coingecko_intelligence_provider() -> CoinGeckoIntelligenceProvider:
    """Return the module-level CoinGeckoIntelligenceProvider singleton."""
    global _provider
    if _provider is None:
        _provider = CoinGeckoIntelligenceProvider()
    return _provider
