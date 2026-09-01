"""
OCI-009 — OnChainAwareAggregator integration tests.

Verifies the full blending pipeline: exchange/macro providers + on-chain
providers merged into one canonical dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intelligence.onchain.schema import ONCHAIN_NEUTRAL
from src.intelligence.providers.aggregator import OnChainAwareAggregator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_exchange_provider(exchange_id: str, metrics: dict) -> MagicMock:
    p = MagicMock()
    p.exchange_id = exchange_id
    p.initialize = AsyncMock()
    p.close = AsyncMock()
    p.fetch_metrics = AsyncMock(return_value=metrics)
    return p


def _mock_onchain_provider(exchange_id: str, metrics: dict) -> MagicMock:
    p = MagicMock()
    p.exchange_id = exchange_id
    p.initialize = AsyncMock()
    p.close = AsyncMock()
    p.fetch_metrics = AsyncMock(return_value=metrics)
    return p


def _base_exchange_metrics(confidence: float = 0.8) -> dict:
    return {
        "binance_funding_rate_pct": 0.01,
        "futures_oi_change_pct": 5.0,
        "cross_exchange_basis_spread_bps": 10.0,
        "whale_buy_sell_ratio": 1.2,
        "liquidation_pressure_24h_zscore": 0.5,
        "liquidation_cascade_risk_usd": 1e6,
        "exchange_stress_score": 0.3,
        "exchange_netflow_7d_zscore": 0.0,
        "exchange_reserve_ratio": 0.5,
        "miner_netflow_signal": 0.0,
        "staking_unlock_risk": 0.0,
        "entity_exchange_imbalance": 0.0,
        "btc_dominance_regime": 0.0,
        "stablecoin_reserve_ratio": 0.5,
        "network_activity_score": 0.0,
        "confidence": confidence,
        "timestamp": 1_700_000_000.0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnChainAwareAggregatorNoOnChain:
    @pytest.mark.asyncio
    async def test_without_onchain_providers(self):
        """Falls back to exchange/macro result unchanged."""
        ep = _mock_exchange_provider("binance", _base_exchange_metrics())
        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[],
        )
        # Patch super().fetch_metrics to return known base
        base = _base_exchange_metrics()
        with patch.object(type(agg).__mro__[1], "fetch_metrics", new=AsyncMock(return_value=base)):
            result = await agg.fetch_metrics()
        assert result["binance_funding_rate_pct"] == pytest.approx(0.01)


class TestOnChainAwareAggregatorBlending:
    @pytest.mark.asyncio
    async def test_onchain_non_neutral_blended(self):
        """Non-neutral on-chain value blends into base dict."""
        ep = _mock_exchange_provider("binance", _base_exchange_metrics(0.8))
        oc_metrics = dict(ONCHAIN_NEUTRAL)
        oc_metrics["confidence"] = 0.9
        oc_metrics["timestamp"] = 1_700_000_001.0
        oc_metrics["exchange_netflow_7d_zscore"] = 2.5
        oc_metrics["exchange_reserve_ratio"] = 0.35
        oc_prov = _mock_onchain_provider("cryptoquant", oc_metrics)

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov],
        )
        base = _base_exchange_metrics(0.8)
        with patch.object(type(agg).__mro__[1], "fetch_metrics", new=AsyncMock(return_value=base)):
            result = await agg.fetch_metrics()

        # On-chain gated field with confidence > 0 should be blended in
        assert result["exchange_netflow_7d_zscore"] != 0.0
        assert result["exchange_reserve_ratio"] != pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_gated_field_blocked_when_no_confidence(self):
        """Gated fields stay at neutral when on-chain confidence=0."""
        ep = _mock_exchange_provider("binance", _base_exchange_metrics(0.8))
        oc_metrics = dict(ONCHAIN_NEUTRAL)
        oc_metrics["confidence"] = 0.0  # disabled provider
        oc_metrics["exchange_netflow_7d_zscore"] = 2.5  # should be blocked
        oc_prov = _mock_onchain_provider("cryptoquant", oc_metrics)

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov],
        )
        base = _base_exchange_metrics(0.8)
        with patch.object(type(agg).__mro__[1], "fetch_metrics", new=AsyncMock(return_value=base)):
            result = await agg.fetch_metrics()

        assert result["exchange_netflow_7d_zscore"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_failed_onchain_provider_skipped(self):
        """Exception from on-chain provider is swallowed; base unchanged."""
        ep = _mock_exchange_provider("binance", _base_exchange_metrics(0.8))
        oc_prov = _mock_onchain_provider("cryptoquant", {})
        oc_prov.fetch_metrics = AsyncMock(side_effect=RuntimeError("network error"))

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov],
        )
        base = _base_exchange_metrics(0.8)
        with patch.object(type(agg).__mro__[1], "fetch_metrics", new=AsyncMock(return_value=base)):
            result = await agg.fetch_metrics()  # must not raise

        assert result["confidence"] == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_initialize_all_calls_onchain_providers(self):
        ep = _mock_exchange_provider("binance", {})
        oc_prov = _mock_onchain_provider("cryptoquant", {})

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov],
        )

        with patch.object(type(agg).__mro__[1], "initialize_all", new=AsyncMock()):
            await agg.initialize_all()

        oc_prov.initialize.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_all_calls_onchain_providers(self):
        ep = _mock_exchange_provider("binance", {})
        oc_prov = _mock_onchain_provider("coinglass", {})

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov],
        )

        with patch.object(type(agg).__mro__[1], "close_all", new=AsyncMock()):
            await agg.close_all()

        oc_prov.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_onchain_providers_merged(self):
        """Two on-chain providers; results confidence-blended."""
        ep = _mock_exchange_provider("binance", _base_exchange_metrics(0.8))

        oc1 = dict(ONCHAIN_NEUTRAL)
        oc1["confidence"] = 1.0
        oc1["timestamp"] = 1_700_000_001.0
        oc1["exchange_netflow_7d_zscore"] = 2.0
        oc_prov1 = _mock_onchain_provider("cryptoquant", oc1)

        oc2 = dict(ONCHAIN_NEUTRAL)
        oc2["confidence"] = 0.5
        oc2["timestamp"] = 1_700_000_002.0
        oc2["exchange_netflow_7d_zscore"] = 1.0
        oc_prov2 = _mock_onchain_provider("coinglass", oc2)

        agg = OnChainAwareAggregator(
            exchange_providers=[ep],
            macro_providers=[],
            onchain_providers=[oc_prov1, oc_prov2],
        )
        base = _base_exchange_metrics(0.8)
        with patch.object(type(agg).__mro__[1], "fetch_metrics", new=AsyncMock(return_value=base)):
            result = await agg.fetch_metrics()

        # Blended on-chain value should be between 1.0 and 2.0
        z = result["exchange_netflow_7d_zscore"]
        assert 1.0 <= z <= 2.0
