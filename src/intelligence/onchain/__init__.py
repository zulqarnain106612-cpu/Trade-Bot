"""
On-chain intelligence providers.

OCI-001: Foundation layer — RateLimiter, CircuitBreaker, AsyncHTTPCache, OnChainProvider ABC.
OCI-002: ArkhamProvider (exchange flows, whale signals).
OCI-003: DeFiLlamaProvider (staking unlock risk, TVL).
OCI-004: DuneProvider (miner flows, entity exchange imbalance).
OCI-005: CryptoQuantProvider (exchange reserve, netflow z-score, MVRV).
OCI-006: CoinglassProvider (OI change, liquidation heatmap, L/S ratio).
OCI-007: Canonical schema + merge helpers.
"""

from src.intelligence.onchain.arkham_provider import ArkhamProvider
from src.intelligence.onchain.base import (
    AsyncHTTPCache,
    CircuitBreaker,
    CircuitOpenError,
    OnChainProvider,
    RateLimiter,
)
from src.intelligence.onchain.coinglass_provider import CoinglassProvider
from src.intelligence.onchain.cryptoquant_provider import CryptoQuantProvider
from src.intelligence.onchain.defillama_provider import DeFiLlamaProvider
from src.intelligence.onchain.dune_provider import DuneProvider
from src.intelligence.onchain.schema import (
    ALL_FIELDS,
    GATED_FIELDS,
    ONCHAIN_NEUTRAL,
    merge_onchain_results,
    validate_provider_result,
)

__all__ = [
    # Schema
    "ALL_FIELDS",
    "GATED_FIELDS",
    "ONCHAIN_NEUTRAL",
    # Providers
    "ArkhamProvider",
    # Foundation
    "AsyncHTTPCache",
    "CircuitBreaker",
    "CircuitOpenError",
    "CoinglassProvider",
    "CryptoQuantProvider",
    "DeFiLlamaProvider",
    "DuneProvider",
    "OnChainProvider",
    "RateLimiter",
    "merge_onchain_results",
    "validate_provider_result",
]
