"""
Multi-provider intelligence aggregator.

Merges outputs from all configured ExchangeIntelligenceProviders into a
single canonical dict matching IntelligenceMetrics field names.

Merge strategy per field:
  - Exchange-specific fields (funding_rate, OI, basis, whale_ratio,
    exchange_stress): weighted average across exchange providers that
    returned real data (confidence-weighted; neutral defaults excluded).
  - Cross-market fields (btc_dominance, stablecoin_ratio,
    network_activity): taken from the dedicated provider (CoinGecko /
    blockchain.info); these are not exchange-specific.
  - Glassnode-gated fields (exchange_netflow, reserve_ratio,
    miner_netflow, staking_unlock, entity_exchange_imbalance): remain
    NaN until paid API keys are provisioned (GAP-015).

Extensibility:
  - Add a new provider by implementing ExchangeIntelligenceProvider and
    passing it to MultiProviderIntelligenceAggregator.__init__().
  - The aggregator auto-classifies fields as "exchange" or "cross-market"
    based on which provider sets them non-neutral.

Confidence policy:
  - Final confidence = weighted mean of per-provider confidences,
    weighted by number of fields each provider owns.
  - Glassnode-gated NaN fields further reduce confidence by the same
    CONFIDENCE_PENALTY used in binance_provider.py.

Authority:
  López de Prado (2018) AFML — ensemble feature construction.
  Almgren & Chriss (2001) — cross-venue execution cost signals.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from typing import Final

import structlog

from src.intelligence.providers.base import ExchangeIntelligenceProvider


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Neutral value per field (used to detect whether a provider actually populated a field)
_NEUTRAL: Final[dict[str, float]] = {
    "binance_funding_rate_pct":        0.0,
    "futures_oi_change_pct":           0.0,
    "cross_exchange_basis_spread_bps": 0.0,
    "whale_buy_sell_ratio":            1.0,
    "liquidation_pressure_24h_zscore": 0.0,
    "liquidation_cascade_risk_usd":    0.0,
    "exchange_stress_score":           0.0,
    "exchange_netflow_7d_zscore":      0.0,
    "exchange_reserve_ratio":          0.5,
    "miner_netflow_signal":            0.0,
    "staking_unlock_risk":             0.0,
    "entity_exchange_imbalance":       0.0,
    "btc_dominance_regime":            0.0,
    "stablecoin_reserve_ratio":        0.5,
    "network_activity_score":          0.0,
}

# Fields that, when still at neutral, represent genuinely missing paid-source data
# and should push final confidence below the exchange-derived floor.
_PAID_GATED_FIELDS: Final[frozenset[str]] = frozenset({
    "exchange_netflow_7d_zscore",
    "exchange_reserve_ratio",
    "miner_netflow_signal",
    "staking_unlock_risk",
    "entity_exchange_imbalance",
})

_CONFIDENCE_PENALTY_PER_MISSING: Final[float] = 0.05

# Fields owned by cross-market providers (not exchange-specific)
_CROSS_MARKET_FIELDS: Final[frozenset[str]] = frozenset({
    "btc_dominance_regime",
    "stablecoin_reserve_ratio",
    "network_activity_score",
})

# Fields that come from exchange providers
_EXCHANGE_FIELDS: Final[frozenset[str]] = frozenset(_NEUTRAL.keys()) - _CROSS_MARKET_FIELDS - _PAID_GATED_FIELDS


class MultiProviderIntelligenceAggregator:
    """
    Aggregates intelligence metrics from multiple providers into one dict.

    Args:
        exchange_providers: Providers tied to specific exchanges (Binance, OKX, …).
                            These supply trading microstructure data.
        macro_providers:    Exchange-agnostic providers (CoinGecko, blockchain.info).
                            These supply cross-market and on-chain data.

    Usage::

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[binance_prov, okx_prov],
            macro_providers=[coingecko_prov, blockchain_prov],
        )
        await agg.initialize_all()
        metrics = await agg.fetch_metrics()
        await agg.close_all()
    """

    def __init__(
        self,
        exchange_providers: Sequence[ExchangeIntelligenceProvider],
        macro_providers: Sequence[ExchangeIntelligenceProvider],
    ) -> None:
        self._exchange_providers = list(exchange_providers)
        self._macro_providers = list(macro_providers)
        self._all_providers: list[ExchangeIntelligenceProvider] = (
            self._exchange_providers + self._macro_providers
        )
        self._log = log.bind(
            component="multi_provider_aggregator",
            exchange_providers=[p.exchange_id for p in self._exchange_providers],
            macro_providers=[p.exchange_id for p in self._macro_providers],
        )

    async def initialize_all(self) -> None:
        """Initialize all providers concurrently. Logs and continues on partial failure."""
        results = await asyncio.gather(
            *[p.initialize() for p in self._all_providers],
            return_exceptions=True,
        )
        for provider, result in zip(self._all_providers, results, strict=False):
            if isinstance(result, Exception):
                self._log.warning(
                    "aggregator.provider_init_failed",
                    exchange_id=provider.exchange_id,
                    error=str(result),
                )

    async def close_all(self) -> None:
        """Close all providers concurrently."""
        await asyncio.gather(
            *[p.close() for p in self._all_providers],
            return_exceptions=True,
        )

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch from all providers concurrently and merge into one dict.

        Merge rules:
          Exchange fields (funding, OI, basis, stress, whale, liquidation):
            confidence-weighted mean across providers that returned non-neutral values.
            If all providers return neutral, the field stays at neutral (not NaN).
          Cross-market fields (btc_dominance, stablecoin, network_activity):
            Taken from the macro provider with highest confidence that set a non-neutral value.
          Paid-gated fields (Glassnode):
            Remain at neutral (0.0 / 0.5) pending key provisioning (GAP-015).
            Each such field still at neutral reduces final confidence.

        Never raises.
        """
        ts = int(time.time())

        # Fan-out: all providers in parallel
        all_results = await asyncio.gather(
            *[p.fetch_metrics() for p in self._all_providers],
            return_exceptions=True,
        )

        exchange_results: list[dict[str, float]] = []
        macro_results: list[dict[str, float]] = []

        for provider, result in zip(self._all_providers, all_results, strict=False):
            if isinstance(result, Exception):
                self._log.warning(
                    "aggregator.provider_fetch_failed",
                    exchange_id=provider.exchange_id,
                    error=str(result),
                )
                continue
            if provider in self._exchange_providers:
                exchange_results.append(result)
            else:
                macro_results.append(result)

        merged: dict[str, float] = dict(_NEUTRAL)  # start from neutrals

        # --- Merge exchange fields (confidence-weighted mean) ---
        for field in _EXCHANGE_FIELDS:
            values: list[float] = []
            weights: list[float] = []
            for r in exchange_results:
                v = r.get(field, _NEUTRAL[field])
                if not _is_neutral(field, v):
                    values.append(v)
                    weights.append(r.get("confidence", 0.5))
            if values and weights:
                total_w = sum(weights)
                merged[field] = sum(v * w for v, w in zip(values, weights, strict=False)) / total_w

        # --- Merge cross-market fields (best-confidence macro provider) ---
        for field in _CROSS_MARKET_FIELDS:
            best_val: float | None = None
            best_conf: float = -1.0
            for r in macro_results:
                v = r.get(field, _NEUTRAL[field])
                c = r.get("confidence", 0.5)
                if not _is_neutral(field, v) and c > best_conf:
                    best_val = v
                    best_conf = c
            if best_val is not None:
                merged[field] = best_val

        # --- Confidence: mean of all provider confidences, then penalise paid gaps ---
        all_confs = [r.get("confidence", 0.5) for r in exchange_results + macro_results]
        base_conf = sum(all_confs) / len(all_confs) if all_confs else 0.4

        # Penalise for each paid-gated field still at neutral
        paid_missing = sum(
            1 for f in _PAID_GATED_FIELDS
            if _is_neutral(f, merged.get(f, _NEUTRAL[f]))
        )
        final_conf = max(0.0, min(1.0, base_conf - paid_missing * _CONFIDENCE_PENALTY_PER_MISSING))

        merged["confidence"] = final_conf
        merged["timestamp"] = float(ts)

        self._log.debug(
            "aggregator.merged",
            confidence=final_conf,
            exchange_providers=len(exchange_results),
            macro_providers=len(macro_results),
            paid_missing=paid_missing,
        )
        return merged


def _is_neutral(field: str, value: float) -> bool:
    """True if value equals the neutral default for this field."""
    neutral = _NEUTRAL.get(field, 0.0)
    if math.isnan(value):
        return False   # NaN is not neutral — it's missing
    return abs(value - neutral) < 1e-9


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_aggregator: MultiProviderIntelligenceAggregator | None = None


def get_multi_provider_aggregator(
    symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT:USDT",
) -> MultiProviderIntelligenceAggregator:
    """
    Return the module-level MultiProviderIntelligenceAggregator singleton.

    Lazily constructs and wires all configured providers.
    symbol/perp_symbol are only used on first call.
    """
    global _aggregator
    if _aggregator is None:
        from src.intelligence.providers.binance_provider import get_binance_intelligence_provider
        from src.intelligence.providers.blockchain_provider import (
            get_blockchain_intelligence_provider,
        )
        from src.intelligence.providers.coingecko_provider import (
            get_coingecko_intelligence_provider,
        )
        from src.intelligence.providers.okx_provider import get_okx_intelligence_provider

        _aggregator = MultiProviderIntelligenceAggregator(
            exchange_providers=[
                get_binance_intelligence_provider(symbol=symbol, perp_symbol=perp_symbol),
                get_okx_intelligence_provider(symbol=symbol, perp_symbol=perp_symbol),
            ],
            macro_providers=[
                get_coingecko_intelligence_provider(),
                get_blockchain_intelligence_provider(),
            ],
        )
    return _aggregator


# ---------------------------------------------------------------------------
# OCI-008: OnChain provider integration
# ---------------------------------------------------------------------------

from src.intelligence.onchain.base import OnChainProvider  # noqa: E402
from src.intelligence.onchain.schema import (  # noqa: E402
    GATED_FIELDS,
    ONCHAIN_NEUTRAL,
    merge_onchain_results,
    validate_provider_result,
)


class OnChainAwareAggregator(MultiProviderIntelligenceAggregator):
    """
    Extends MultiProviderIntelligenceAggregator with on-chain provider support.

    On-chain results are merged via schema.merge_onchain_results() then blended
    into the exchange/macro merged dict using confidence-weighted mean per field.

    Gate policy (OCI-010):
      Fields in GATED_FIELDS remain at neutral until their on-chain provider
      returns confidence > 0 (i.e. an API key is present and the call succeeded).
    """

    def __init__(
        self,
        exchange_providers: "Sequence[ExchangeIntelligenceProvider]",
        macro_providers: "Sequence[ExchangeIntelligenceProvider]",
        onchain_providers: "Sequence[OnChainProvider] | None" = None,
    ) -> None:
        super().__init__(exchange_providers, macro_providers)
        self._onchain_providers: list[OnChainProvider] = list(onchain_providers or [])

    async def initialize_all(self) -> None:
        await super().initialize_all()
        oc_results = await asyncio.gather(
            *[p.initialize() for p in self._onchain_providers],
            return_exceptions=True,
        )
        for provider, result in zip(self._onchain_providers, oc_results, strict=False):
            if isinstance(result, Exception):
                self._log.warning(
                    "aggregator.onchain_init_failed",
                    exchange_id=provider.exchange_id,
                    error=str(result),
                )

    async def close_all(self) -> None:
        await super().close_all()
        await asyncio.gather(
            *[p.close() for p in self._onchain_providers],
            return_exceptions=True,
        )

    async def fetch_metrics(self) -> dict[str, float]:
        """Fetch exchange/macro + on-chain concurrently; merge all into one dict."""
        exchange_macro_task = asyncio.create_task(super().fetch_metrics())
        onchain_tasks = [
            asyncio.create_task(p.fetch_metrics()) for p in self._onchain_providers
        ]

        base = await exchange_macro_task
        raw_onchain = await asyncio.gather(*onchain_tasks, return_exceptions=True)

        # Validate + collect clean on-chain results
        clean_onchain: list[dict[str, float]] = []
        for provider, result in zip(self._onchain_providers, raw_onchain, strict=False):
            if isinstance(result, Exception):
                self._log.warning(
                    "aggregator.onchain_fetch_failed",
                    exchange_id=provider.exchange_id,
                    error=str(result),
                )
                continue
            clean = validate_provider_result(result, provider.exchange_id)
            clean_onchain.append(clean)

        if not clean_onchain:
            return base  # no on-chain data; return exchange/macro as-is

        merged_onchain = merge_onchain_results(clean_onchain)

        # Blend on-chain results into base dict (OCI-010 gate policy)
        oc_conf = merged_onchain.get("confidence", 0.0)
        base_conf = base.get("confidence", 0.0)

        for field, oc_val in merged_onchain.items():
            if field in ("confidence", "timestamp"):
                continue
            neutral = ONCHAIN_NEUTRAL.get(field)
            if neutral is None:
                continue  # cross-market field not in schema, skip
            if field in GATED_FIELDS and oc_conf <= 0.0:
                continue  # gated: no key provisioned, leave base value
            if abs(oc_val - neutral) < 1e-9:
                continue  # on-chain returned neutral, do not overwrite
            # Confidence-weighted blend with existing base value
            base_val = base.get(field, neutral)
            if abs(base_val - neutral) < 1e-9 or base_conf <= 0.0:
                base[field] = oc_val
            else:
                total_w = base_conf + oc_conf
                base[field] = (base_val * base_conf + oc_val * oc_conf) / total_w

        # Blend confidences
        if oc_conf > 0.0:
            total = base_conf + oc_conf
            base["confidence"] = total / 2.0 if total > 0 else 0.0

        return base


# ---------------------------------------------------------------------------
# OCI-008: Singleton factory for OnChainAwareAggregator
# ---------------------------------------------------------------------------

_onchain_aware_aggregator: "OnChainAwareAggregator | None" = None


def get_onchain_aware_aggregator(
    symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT:USDT",
) -> "OnChainAwareAggregator":
    """
    Return module-level OnChainAwareAggregator singleton.

    Lazily constructs exchange/macro providers (same as get_multi_provider_aggregator)
    plus all configured on-chain providers.  On-chain providers fail-open when API
    keys are absent — they return neutral + confidence=0.0, and gated fields are not
    written into the merged dict.  Symbol args are only used on first call.
    """
    global _onchain_aware_aggregator
    if _onchain_aware_aggregator is None:
        from src.config import get_settings
        from src.intelligence.onchain.arkham_provider import ArkhamProvider
        from src.intelligence.onchain.coinglass_provider import CoinglassProvider
        from src.intelligence.onchain.cryptoquant_provider import CryptoQuantProvider
        from src.intelligence.onchain.defillama_provider import DeFiLlamaProvider
        from src.intelligence.onchain.dune_provider import DuneProvider
        from src.intelligence.providers.binance_provider import get_binance_intelligence_provider
        from src.intelligence.providers.blockchain_provider import (
            get_blockchain_intelligence_provider,
        )
        from src.intelligence.providers.coingecko_provider import (
            get_coingecko_intelligence_provider,
        )
        from src.intelligence.providers.okx_provider import get_okx_intelligence_provider

        cfg = get_settings().intelligence  # IntelligenceSettings

        _onchain_aware_aggregator = OnChainAwareAggregator(
            exchange_providers=[
                get_binance_intelligence_provider(symbol=symbol, perp_symbol=perp_symbol),
                get_okx_intelligence_provider(symbol=symbol, perp_symbol=perp_symbol),
            ],
            macro_providers=[
                get_coingecko_intelligence_provider(),
                get_blockchain_intelligence_provider(),
            ],
            onchain_providers=[
                ArkhamProvider(
                    api_key=cfg.arkham_api_key,
                    cache_ttl_s=cfg.arkham_cache_ttl_s,
                ),
                DeFiLlamaProvider(),  # public API; no key required
                DuneProvider(
                    api_key=cfg.dune_api_key,
                    cache_ttl_s=cfg.dune_cache_ttl_s,
                ),
                CryptoQuantProvider(
                    api_key=cfg.cryptoquant_api_key,
                ),
                CoinglassProvider(
                    api_key=cfg.coinglass_api_key,
                    cache_ttl_s=cfg.coinglass_cache_ttl_s,
                ),
            ],
        )
    return _onchain_aware_aggregator
