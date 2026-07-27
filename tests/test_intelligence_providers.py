"""
Tests for multi-provider intelligence wiring (GAP-015).

Coverage:
  1. ExchangeIntelligenceProvider ABC — subclass contract enforcement
  2. OKXIntelligenceProvider — fetch_metrics() shape, graceful degradation
  3. CoinGeckoIntelligenceProvider — fetch_metrics() shape, BTC dominance z-score
  4. BlockchainIntelligenceProvider — fetch_metrics() shape, network_activity_score
  5. MultiProviderIntelligenceAggregator — merge logic, confidence weighting,
     cross-market field selection, partial provider failure, paid-gated penalty
  6. pipeline._inject_intelligence_features — NaN rejection, prefix mapping,
     confidence passthrough
  7. pipeline.build_inference_features — intelligence_metrics kwarg integration
  8. get_multi_provider_aggregator — singleton integrity

All network calls are mocked. No live API calls in tests.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_feature_vec() -> pd.Series:
    """Minimal 7-feature base vector matching FEATURE_COLUMNS."""
    from src.features.pipeline import FEATURE_COLUMNS

    return pd.Series(
        {c: float(i) for i, c in enumerate(FEATURE_COLUMNS)},
        dtype=np.float64,
    )


@pytest.fixture()
def full_intel_metrics() -> dict[str, float]:
    """Canonical all-real intelligence metrics dict (as aggregator would return)."""
    return {
        "binance_funding_rate_pct": 0.01,
        "futures_oi_change_pct": 1.5,
        "cross_exchange_basis_spread_bps": 8.0,
        "whale_buy_sell_ratio": 1.3,
        "liquidation_pressure_24h_zscore": 0.5,
        "liquidation_cascade_risk_usd": 500_000.0,
        "exchange_stress_score": 0.25,
        "exchange_netflow_7d_zscore": 0.0,  # paid-gated: neutral
        "exchange_reserve_ratio": 0.5,  # paid-gated: neutral
        "miner_netflow_signal": 0.0,  # paid-gated: neutral
        "staking_unlock_risk": 0.0,  # paid-gated: neutral
        "entity_exchange_imbalance": 0.0,  # paid-gated: neutral
        "btc_dominance_regime": 0.8,
        "stablecoin_reserve_ratio": 0.07,
        "network_activity_score": 0.4,
        # OCI-012 fields
        "defi_tvl_7d_change_pct": 2.5,
        "mvrv_z_score": 0.0,
        "sopr": 0.0,
        "confidence": 0.75,
        "timestamp": float(int(time.time())),
    }


# ---------------------------------------------------------------------------
# 1. ABC enforcement
# ---------------------------------------------------------------------------


class TestExchangeIntelligenceProviderABC:
    def test_cannot_instantiate_abstract(self):
        from src.intelligence.providers.base import ExchangeIntelligenceProvider

        with pytest.raises(TypeError):
            ExchangeIntelligenceProvider()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_all(self):
        from src.intelligence.providers.base import ExchangeIntelligenceProvider

        class Incomplete(ExchangeIntelligenceProvider):
            @property
            def exchange_id(self):
                return "test"

            async def initialize(self):
                pass

            # Missing close() and fetch_metrics()

        with pytest.raises(TypeError):
            Incomplete()

    def test_complete_subclass_instantiates(self):
        from src.intelligence.providers.base import ExchangeIntelligenceProvider

        class Complete(ExchangeIntelligenceProvider):
            @property
            def exchange_id(self):
                return "test"

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def fetch_metrics(self) -> dict[str, float]:
                return {"confidence": 1.0, "timestamp": float(int(time.time()))}

        assert Complete().exchange_id == "test"


# ---------------------------------------------------------------------------
# 2. OKXIntelligenceProvider
# ---------------------------------------------------------------------------


class TestOKXIntelligenceProvider:
    @pytest.fixture()
    def provider(self):
        from src.intelligence.providers.okx_provider import OKXIntelligenceProvider

        p = OKXIntelligenceProvider(symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT")
        return p

    @pytest.mark.asyncio
    async def test_fetch_metrics_returns_all_keys(self, provider):
        """All 15 IntelligenceMetrics fields + confidence + timestamp must be present."""
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

        # Mock all internal fetches
        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.5})
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 2.0, "value_usd": 1e9})
        provider._fetch_basis_data = AsyncMock(return_value=12.5)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.4)

        metrics = await provider.fetch_metrics()

        # All 15 IntelligenceMetrics fields must be present (check by stripping prefix)
        for col in INTELLIGENCE_FEATURE_COLUMNS:
            raw_key = col.removeprefix("intelligence_")
            assert raw_key in metrics, f"Missing key: {raw_key}"
        assert "confidence" in metrics
        assert "timestamp" in metrics

    @pytest.mark.asyncio
    async def test_fetch_metrics_never_raises_on_error(self, provider):
        """Any internal exception must not propagate — degrade gracefully."""
        provider._fetch_funding_data = AsyncMock(side_effect=Exception("network down"))
        provider._fetch_oi_data = AsyncMock(side_effect=RuntimeError("timeout"))
        provider._fetch_basis_data = AsyncMock(side_effect=ValueError("bad data"))
        provider._fetch_whale_taker_ratio = AsyncMock(side_effect=Exception("rate limit"))

        metrics = await provider.fetch_metrics()  # must not raise
        assert isinstance(metrics, dict)
        assert 0.0 <= metrics["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_confidence_reduced_on_partial_failure(self, provider):
        provider._fetch_funding_data = AsyncMock(side_effect=Exception("fail"))
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0})
        provider._fetch_basis_data = AsyncMock(return_value=0.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)

        metrics_partial_fail = await provider.fetch_metrics()

        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.3})
        await provider.fetch_metrics()
        provider._cache.clear()

        # Re-run clean
        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.3})
        metrics_ok = await provider.fetch_metrics()

        assert metrics_partial_fail["confidence"] < metrics_ok["confidence"]

    def test_exchange_id(self, provider):
        assert provider.exchange_id == "okx"

    @pytest.mark.asyncio
    async def test_stress_score_bounded(self, provider):
        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 1.0, "zscore": 5.0})
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": -20.0, "value_usd": 1e10})
        provider._fetch_basis_data = AsyncMock(return_value=200.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)

        metrics = await provider.fetch_metrics()
        assert 0.0 <= metrics["exchange_stress_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_caching_prevents_duplicate_calls(self, provider):
        call_count = 0

        async def counting_network_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Return minimal valid funding rate history (2+ entries required for zscore)
            return [{"fundingRate": 0.0001}, {"fundingRate": 0.0001}]

        # Patch at the network level so the cache logic inside _fetch_funding_data runs.
        provider._perp = AsyncMock()
        provider._perp.fetch_funding_rate_history = counting_network_fetch
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0})
        provider._fetch_basis_data = AsyncMock(return_value=0.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)
        # Ensure a nonzero TTL so the cache entry is valid on the second call.
        provider._cache_ttl = 300

        await provider.fetch_metrics()
        await provider.fetch_metrics()  # second call — funding should hit cache
        assert call_count == 1


# ---------------------------------------------------------------------------
# 2b. BybitIntelligenceProvider
# ---------------------------------------------------------------------------


class TestBybitIntelligenceProvider:
    @pytest.fixture()
    def provider(self):
        from src.intelligence.providers.bybit_provider import BybitIntelligenceProvider

        p = BybitIntelligenceProvider(symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT")
        return p

    @pytest.mark.asyncio
    async def test_fetch_metrics_returns_all_keys(self, provider):
        """All 15 IntelligenceMetrics fields + confidence + timestamp must be present."""
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.5})
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 2.0, "value_usd": 1e9})
        provider._fetch_basis_data = AsyncMock(return_value=12.5)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.4)

        metrics = await provider.fetch_metrics()

        for col in INTELLIGENCE_FEATURE_COLUMNS:
            raw_key = col.removeprefix("intelligence_")
            assert raw_key in metrics, f"Missing key: {raw_key}"
        assert "confidence" in metrics
        assert "timestamp" in metrics

    @pytest.mark.asyncio
    async def test_fetch_metrics_never_raises_on_error(self, provider):
        """Any internal exception must not propagate — degrade gracefully."""
        provider._fetch_funding_data = AsyncMock(side_effect=Exception("network down"))
        provider._fetch_oi_data = AsyncMock(side_effect=RuntimeError("timeout"))
        provider._fetch_basis_data = AsyncMock(side_effect=ValueError("bad data"))
        provider._fetch_whale_taker_ratio = AsyncMock(side_effect=Exception("rate limit"))

        metrics = await provider.fetch_metrics()  # must not raise
        assert isinstance(metrics, dict)
        assert 0.0 <= metrics["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_confidence_reduced_on_partial_failure(self, provider):
        provider._fetch_funding_data = AsyncMock(side_effect=Exception("fail"))
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0})
        provider._fetch_basis_data = AsyncMock(return_value=0.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)

        metrics_partial_fail = await provider.fetch_metrics()

        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.3})
        await provider.fetch_metrics()
        provider._cache.clear()

        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.3})
        metrics_ok = await provider.fetch_metrics()

        assert metrics_partial_fail["confidence"] < metrics_ok["confidence"]

    def test_exchange_id(self, provider):
        assert provider.exchange_id == "bybit"

    @pytest.mark.asyncio
    async def test_stress_score_bounded(self, provider):
        provider._fetch_funding_data = AsyncMock(return_value={"rate_pct": 1.0, "zscore": 5.0})
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": -20.0, "value_usd": 1e10})
        provider._fetch_basis_data = AsyncMock(return_value=200.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)

        metrics = await provider.fetch_metrics()
        assert 0.0 <= metrics["exchange_stress_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_caching_prevents_duplicate_calls(self, provider):
        call_count = 0

        async def counting_network_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return [{"fundingRate": 0.0001}, {"fundingRate": 0.0001}]

        provider._perp = AsyncMock()
        provider._perp.fetch_funding_rate_history = counting_network_fetch
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0})
        provider._fetch_basis_data = AsyncMock(return_value=0.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)
        provider._cache_ttl = 300

        await provider.fetch_metrics()
        await provider.fetch_metrics()
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_whale_ratio_from_taker_trades(self, provider):
        """_fetch_whale_taker_ratio derives buy/sell ratio from ccxt fetch_trades()."""
        provider._perp = AsyncMock()
        provider._perp.fetch_trades = AsyncMock(
            return_value=[
                {"side": "buy", "amount": 3.0},
                {"side": "buy", "amount": 2.0},
                {"side": "sell", "amount": 1.0},
            ]
        )
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_whale_ratio_no_trades_returns_neutral(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_trades = AsyncMock(return_value=[])
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == 1.0

    @pytest.mark.asyncio
    async def test_whale_ratio_all_buys_capped_at_ten(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_trades = AsyncMock(return_value=[{"side": "buy", "amount": 100.0}])
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == 10.0

    @pytest.mark.asyncio
    async def test_whale_ratio_uses_cache(self, provider):
        provider._set_cache(f"whale:{provider._perp_symbol}", 3.5)
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == 3.5

    def test_get_cache_expired(self, provider):
        provider._cache_ttl = 1
        provider._cache["k"] = (time.time() - 10.0, "value")
        assert provider._get_cache("k") is None

    @pytest.mark.asyncio
    async def test_fetch_funding_data_empty_history(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_funding_rate_history = AsyncMock(return_value=[])
        result = await provider._fetch_funding_data()
        assert result == {"rate_pct": 0.0, "zscore": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_funding_data_single_rate_no_zscore(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_funding_rate_history = AsyncMock(
            return_value=[{"fundingRate": 0.0001}]
        )
        result = await provider._fetch_funding_data()
        assert result["rate_pct"] == pytest.approx(0.0001)
        assert result["zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_oi_data_empty_history(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_open_interest_history = AsyncMock(return_value=[])
        result = await provider._fetch_oi_data()
        assert result == {"change_pct": 0.0, "value_usd": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_oi_data_uses_cache(self, provider):
        cached = {"change_pct": 0.03, "value_usd": 1_000_000.0}
        provider._set_cache(f"oi:{provider._perp_symbol}", cached)
        result = await provider._fetch_oi_data()
        assert result == cached

    @pytest.mark.asyncio
    async def test_fetch_oi_data_computes_change_pct(self, provider):
        provider._perp = AsyncMock()
        provider._perp.fetch_open_interest_history = AsyncMock(
            return_value=[
                {"openInterestAmount": 1000.0, "openInterestValue": 5_000_000.0},
                {"openInterestAmount": 1100.0, "openInterestValue": 5_500_000.0},
            ]
        )
        result = await provider._fetch_oi_data()
        assert result["change_pct"] == pytest.approx(10.0)
        assert result["value_usd"] == pytest.approx(5_500_000.0)

    @pytest.mark.asyncio
    async def test_fetch_basis_data_uses_cache(self, provider):
        provider._set_cache(f"basis:{provider._symbol}", 12.5)
        result = await provider._fetch_basis_data()
        assert result == 12.5

    @pytest.mark.asyncio
    async def test_fetch_basis_data_missing_prices_returns_zero(self, provider):
        provider._spot = AsyncMock()
        provider._perp = AsyncMock()
        provider._spot.fetch_ticker = AsyncMock(return_value={"last": None, "close": None})
        provider._perp.fetch_ticker = AsyncMock(return_value={"last": 30_000.0})
        result = await provider._fetch_basis_data()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_fetch_basis_data_computes_bps(self, provider):
        provider._spot = AsyncMock()
        provider._perp = AsyncMock()
        provider._spot.fetch_ticker = AsyncMock(return_value={"last": 30_000.0})
        provider._perp.fetch_ticker = AsyncMock(return_value={"last": 30_030.0})
        result = await provider._fetch_basis_data()
        assert result == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_initialize_and_close(self, provider):
        """
        initialize()/close() call load_markets()/close() on both ccxt clients.

        Mocked rather than hitting the real exchange (consistent with every
        other test in this file) -- a live call to api.bybit.com is unreliable
        in CI: it has been observed failing both from TLS interception in
        sandboxed environments and from Bybit/CloudFront blocking requests
        from some CI runner regions with 403 Forbidden.
        """
        provider._spot.load_markets = AsyncMock(return_value={})
        provider._perp.load_markets = AsyncMock(return_value={})
        provider._spot.close = AsyncMock()
        provider._perp.close = AsyncMock()

        await provider.initialize()
        await provider.close()

        provider._spot.load_markets.assert_awaited_once()
        provider._perp.load_markets.assert_awaited_once()
        provider._spot.close.assert_awaited_once()
        provider._perp.close.assert_awaited_once()

    def test_singleton(self):
        import src.intelligence.providers.bybit_provider as mod

        mod._provider = None
        from src.intelligence.providers.bybit_provider import get_bybit_intelligence_provider

        p1 = get_bybit_intelligence_provider()
        p2 = get_bybit_intelligence_provider()
        assert p1 is p2
        mod._provider = None


# ---------------------------------------------------------------------------
# 3. CoinGeckoIntelligenceProvider
# ---------------------------------------------------------------------------


class TestCoinGeckoIntelligenceProvider:
    @pytest.fixture()
    def provider(self):
        from src.intelligence.providers.coingecko_provider import CoinGeckoIntelligenceProvider

        return CoinGeckoIntelligenceProvider(cache_ttl_s=0)  # ttl=0 disables cache for tests

    @pytest.mark.asyncio
    async def test_btc_dominance_zscore_computed(self, provider):
        """After several calls with varying BTC dominance, z-score should be non-zero."""
        mock_data = {
            "market_cap_percentage": {
                "btc": 50.0,
                "usdt": 5.0,
                "usdc": 2.0,
                "dai": 0.5,
                "frax": 0.1,
            },
            "total_market_cap": {"usd": 1e12},
        }

        async def fake_fetch():
            return mock_data

        provider._fetch_global = fake_fetch

        # Seed history with varying values so z-score is computable
        provider._btc_dom_history = [45.0, 47.0, 48.0, 46.0, 49.0, 51.0, 52.0]
        mock_data["market_cap_percentage"]["btc"] = 55.0  # outlier → positive z-score

        metrics = await provider.fetch_metrics()
        assert "btc_dominance_regime" in metrics
        # z-score of outlier should be > 0
        assert metrics["btc_dominance_regime"] > 0.0

    @pytest.mark.asyncio
    async def test_btc_dominance_history_truncated_to_window(self, provider):
        from src.intelligence.providers.coingecko_provider import _BTC_DOM_WINDOW

        mock_data = {
            "market_cap_percentage": {"btc": 50.0},
            "total_market_cap": {"usd": 1e12},
        }

        async def fake_fetch():
            return mock_data

        provider._fetch_global = fake_fetch
        provider._btc_dom_history = [45.0] * _BTC_DOM_WINDOW  # already at cap

        await provider.fetch_metrics()
        assert len(provider._btc_dom_history) == _BTC_DOM_WINDOW

    @pytest.mark.asyncio
    async def test_stablecoin_ratio_bounded(self, provider):
        async def fake_fetch():
            return {
                "market_cap_percentage": {
                    "btc": 40.0,
                    "usdt": 8.0,
                    "usdc": 4.0,
                    "dai": 1.0,
                    "frax": 0.5,
                },
                "total_market_cap": {"usd": 2e12},
            }

        provider._fetch_global = fake_fetch
        metrics = await provider.fetch_metrics()
        ratio = metrics["stablecoin_reserve_ratio"]
        assert 0.0 <= ratio <= 1.0

    @pytest.mark.asyncio
    async def test_never_raises_on_network_error(self, provider):
        async def fail():
            raise ConnectionError("CoinGecko unreachable")

        provider._fetch_global = fail
        metrics = await provider.fetch_metrics()
        assert isinstance(metrics, dict)
        assert metrics["btc_dominance_regime"] == 0.0
        assert metrics["confidence"] < 1.0

    def test_exchange_id(self, provider):
        assert provider.exchange_id == "coingecko"

    @pytest.mark.asyncio
    async def test_all_intel_fields_present(self, provider):
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

        async def fake_fetch():
            return {
                "market_cap_percentage": {
                    "btc": 48.0,
                    "usdt": 6.0,
                    "usdc": 3.0,
                    "dai": 0.5,
                    "frax": 0.2,
                },
                "total_market_cap": {"usd": 1.5e12},
            }

        provider._fetch_global = fake_fetch
        metrics = await provider.fetch_metrics()
        for col in INTELLIGENCE_FEATURE_COLUMNS:
            raw_key = col.removeprefix("intelligence_")
            assert raw_key in metrics


# ---------------------------------------------------------------------------
# 4. BlockchainIntelligenceProvider
# ---------------------------------------------------------------------------


class TestBlockchainIntelligenceProvider:
    @pytest.fixture()
    def provider(self):
        from src.intelligence.providers.blockchain_provider import BlockchainIntelligenceProvider

        return BlockchainIntelligenceProvider(cache_ttl_s=0)

    @pytest.mark.asyncio
    async def test_network_activity_zscore_computable(self, provider):
        """Seed history so z-score is non-zero."""
        provider._hashrate_history = [100.0, 105.0, 98.0, 102.0, 99.0, 103.0, 101.0]
        provider._tx_history = [200_000.0] * 7

        async def fake_fetch():
            return {"hash_rate": 130.0, "n_tx": 200_000.0}  # hash rate outlier

        provider._fetch_stats = fake_fetch
        metrics = await provider.fetch_metrics()
        # hash_rate 130 vs mean ~101 → positive z-score → network_activity_score > 0
        assert metrics["network_activity_score"] > 0.0

    @pytest.mark.asyncio
    async def test_network_activity_bounded(self, provider):
        provider._hashrate_history = [100.0] * 10
        provider._tx_history = [200_000.0] * 10

        async def fake_fetch():
            return {"hash_rate": 99.9, "n_tx": 200_000.0}

        provider._fetch_stats = fake_fetch
        metrics = await provider.fetch_metrics()
        assert -1.0 <= metrics["network_activity_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_never_raises_on_network_error(self, provider):
        async def fail():
            raise OSError("blockchain.info unreachable")

        provider._fetch_stats = fail
        metrics = await provider.fetch_metrics()
        assert isinstance(metrics, dict)
        assert metrics["network_activity_score"] == 0.0

    def test_exchange_id(self, provider):
        assert provider.exchange_id == "blockchain_info"


# ---------------------------------------------------------------------------
# 5. MultiProviderIntelligenceAggregator
# ---------------------------------------------------------------------------


class TestMultiProviderIntelligenceAggregator:
    def _make_exchange_provider(self, exchange_id: str, metrics: dict) -> Any:
        from src.intelligence.providers.base import ExchangeIntelligenceProvider

        class MockProvider(ExchangeIntelligenceProvider):
            @property
            def exchange_id(self):
                return exchange_id

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def fetch_metrics(self) -> dict[str, float]:
                return metrics

        return MockProvider()

    def test_is_neutral_nan_is_not_neutral(self):
        from src.intelligence.providers.aggregator import _is_neutral

        assert _is_neutral("futures_oi_change_pct", float("nan")) is False

    @pytest.mark.asyncio
    async def test_initialize_all_logs_and_continues_on_partial_failure(self):
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        good = self._make_exchange_provider("binance", {})
        bad = self._make_exchange_provider("okx", {})
        bad.initialize = AsyncMock(side_effect=RuntimeError("init blew up"))

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[good, bad],
            macro_providers=[],
        )
        await agg.initialize_all()  # must not raise despite bad's failure

    def _make_onchain_provider(self, exchange_id: str, metrics: dict) -> Any:
        from src.intelligence.onchain.base import OnChainProvider

        class MockOnChainProvider(OnChainProvider):
            @property
            def exchange_id(self):
                return exchange_id

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def fetch_metrics(self) -> dict[str, float]:
                return metrics

        return MockOnChainProvider.__new__(MockOnChainProvider)  # bypass __init__ (needs no HTTP)

    @pytest.mark.asyncio
    async def test_onchain_aware_initialize_all_logs_on_partial_failure(self):
        from src.intelligence.providers.aggregator import OnChainAwareAggregator

        good_exchange = self._make_exchange_provider("binance", {})
        good_onchain = self._make_onchain_provider("defillama", {})
        bad_onchain = self._make_onchain_provider("arkham_intel", {})
        bad_onchain.initialize = AsyncMock(side_effect=RuntimeError("onchain init blew up"))

        agg = OnChainAwareAggregator(
            exchange_providers=[good_exchange],
            macro_providers=[],
            onchain_providers=[good_onchain, bad_onchain],
        )
        await agg.initialize_all()  # must not raise despite one provider failing

    @pytest.mark.asyncio
    async def test_onchain_aware_close_all_closes_onchain_providers_too(self):
        from src.intelligence.providers.aggregator import OnChainAwareAggregator

        exchange = self._make_exchange_provider("binance", {})
        exchange.close = AsyncMock()
        onchain = self._make_onchain_provider("arkham_intel", {})
        onchain.close = AsyncMock()

        agg = OnChainAwareAggregator(
            exchange_providers=[exchange],
            macro_providers=[],
            onchain_providers=[onchain],
        )
        await agg.close_all()
        exchange.close.assert_awaited_once()
        onchain.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_onchain_aware_fetch_metrics_logs_and_skips_failed_provider(self):
        from src.intelligence.providers.aggregator import OnChainAwareAggregator

        onchain = self._make_onchain_provider("arkham_intel", {})
        onchain.fetch_metrics = AsyncMock(side_effect=RuntimeError("onchain fetch blew up"))

        agg = OnChainAwareAggregator(
            exchange_providers=[self._make_exchange_provider("binance", {})],
            macro_providers=[],
            onchain_providers=[onchain],
        )
        merged = await agg.fetch_metrics()  # must not raise; falls back to base only
        assert isinstance(merged, dict)

    @pytest.mark.asyncio
    async def test_onchain_aware_fetch_metrics_overwrites_neutral_base_value(self):
        """base has no real value for a field (still at its neutral default)
        -- the on-chain value must simply overwrite it, not weighted-blend."""
        from src.intelligence.providers.aggregator import OnChainAwareAggregator

        onchain_metrics = {
            "futures_oi_change_pct": 4.0,
            "confidence": 0.6,
            "timestamp": float(int(time.time())),
        }
        agg = OnChainAwareAggregator(
            exchange_providers=[self._make_exchange_provider("binance", {})],
            macro_providers=[],
            onchain_providers=[self._make_onchain_provider("arkham_intel", onchain_metrics)],
        )
        merged = await agg.fetch_metrics()
        assert merged["futures_oi_change_pct"] == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_onchain_aware_fetch_metrics_blends_confident_values(self):
        """When both base (exchange/macro) and on-chain results are
        non-neutral with positive confidence, the merge must be a genuine
        confidence-weighted average, not just an overwrite."""
        from src.intelligence.providers.aggregator import OnChainAwareAggregator

        exchange_metrics = {
            "futures_oi_change_pct": 2.0,
            "confidence": 0.8,
            "timestamp": float(int(time.time())),
        }
        onchain_metrics = {
            "futures_oi_change_pct": 4.0,
            "confidence": 0.6,
            "timestamp": float(int(time.time())),
        }

        agg = OnChainAwareAggregator(
            exchange_providers=[self._make_exchange_provider("binance", exchange_metrics)],
            macro_providers=[],
            onchain_providers=[self._make_onchain_provider("arkham_intel", onchain_metrics)],
        )
        merged = await agg.fetch_metrics()
        # Confidence-weighted blend of base (2.0) and on-chain (4.0) -- base's
        # own confidence is itself recomputed by the exchange-merge step
        # (penalised for paid-gated fields), not a raw passthrough of 0.8, so
        # this asserts genuine blending occurred rather than an exact formula.
        assert 2.0 < merged["futures_oi_change_pct"] < 4.0

    @pytest.mark.asyncio
    async def test_close_all_closes_every_provider(self):
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        p1 = self._make_exchange_provider("binance", {})
        p2 = self._make_exchange_provider("okx", {})
        p1.close = AsyncMock()
        p2.close = AsyncMock()

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[p1, p2],
            macro_providers=[],
        )
        await agg.close_all()
        p1.close.assert_awaited_once()
        p2.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_intel_fields_present_in_merged(self):
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        binance_metrics = {
            "binance_funding_rate_pct": 0.01,
            "futures_oi_change_pct": 1.5,
            "cross_exchange_basis_spread_bps": 8.0,
            "whale_buy_sell_ratio": 1.3,
            "liquidation_pressure_24h_zscore": 0.5,
            "liquidation_cascade_risk_usd": 5e5,
            "exchange_stress_score": 0.25,
            "exchange_netflow_7d_zscore": 0.0,
            "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0,
            "staking_unlock_risk": 0.0,
            "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0,
            "stablecoin_reserve_ratio": 0.5,
            "network_activity_score": 0.0,
            "defi_tvl_7d_change_pct": 0.0,
            "mvrv_z_score": 0.0,
            "sopr": 0.0,
            "confidence": 0.75,
            "timestamp": float(int(time.time())),
        }
        macro_metrics = {
            **{k: 0.0 for k in binance_metrics},
            "whale_buy_sell_ratio": 1.0,
            "exchange_reserve_ratio": 0.5,
            "btc_dominance_regime": 0.9,
            "stablecoin_reserve_ratio": 0.08,
            "network_activity_score": 0.4,
            "confidence": 0.90,
            "timestamp": float(int(time.time())),
        }

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[self._make_exchange_provider("binance", binance_metrics)],
            macro_providers=[self._make_exchange_provider("coingecko", macro_metrics)],
        )

        result = await agg.fetch_metrics()

        for col in INTELLIGENCE_FEATURE_COLUMNS:
            raw_key = col.removeprefix("intelligence_")
            assert raw_key in result, f"Merged result missing: {raw_key}"
        assert "confidence" in result
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_cross_market_field_comes_from_macro_provider(self):
        """btc_dominance_regime must be taken from macro provider, not exchange."""
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        def _base(exchange_id, btc_dom, conf):
            return {
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
                "btc_dominance_regime": btc_dom,
                "stablecoin_reserve_ratio": 0.5,
                "network_activity_score": 0.0,
                "confidence": conf,
                "timestamp": float(int(time.time())),
            }

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[
                self._make_exchange_provider("binance", _base("binance", 0.0, 0.75))
            ],
            macro_providers=[
                self._make_exchange_provider("coingecko", _base("coingecko", 1.5, 0.90))
            ],
        )
        result = await agg.fetch_metrics()
        # Macro provider's btc_dominance_regime=1.5 must win over exchange neutral=0.0
        assert result["btc_dominance_regime"] == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_exchange_fields_confidence_weighted(self):
        """Exchange fields: confidence-weighted mean across two providers."""
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        def _base(stress, conf):
            return {
                "binance_funding_rate_pct": 0.01,
                "futures_oi_change_pct": 1.0,
                "cross_exchange_basis_spread_bps": 5.0,
                "whale_buy_sell_ratio": 1.2,
                "liquidation_pressure_24h_zscore": 0.3,
                "liquidation_cascade_risk_usd": 1e5,
                "exchange_stress_score": stress,
                "exchange_netflow_7d_zscore": 0.0,
                "exchange_reserve_ratio": 0.5,
                "miner_netflow_signal": 0.0,
                "staking_unlock_risk": 0.0,
                "entity_exchange_imbalance": 0.0,
                "btc_dominance_regime": 0.0,
                "stablecoin_reserve_ratio": 0.5,
                "network_activity_score": 0.0,
                "confidence": conf,
                "timestamp": float(int(time.time())),
            }

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[
                self._make_exchange_provider("binance", _base(0.2, 0.8)),
                self._make_exchange_provider("okx", _base(0.6, 0.4)),
            ],
            macro_providers=[],
        )
        result = await agg.fetch_metrics()
        # Weighted mean: (0.2*0.8 + 0.6*0.4) / (0.8+0.4) = (0.16+0.24)/1.2 = 0.333...
        expected = (0.2 * 0.8 + 0.6 * 0.4) / (0.8 + 0.4)
        assert result["exchange_stress_score"] == pytest.approx(expected, abs=1e-6)

    @pytest.mark.asyncio
    async def test_partial_provider_failure_continues(self):
        """If one provider raises, others still contribute."""
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator

        failing = self._make_exchange_provider("failing", {})
        failing.fetch_metrics = AsyncMock(side_effect=RuntimeError("exploded"))

        good_metrics = {
            "binance_funding_rate_pct": 0.02,
            "futures_oi_change_pct": 0.5,
            "cross_exchange_basis_spread_bps": 3.0,
            "whale_buy_sell_ratio": 1.1,
            "liquidation_pressure_24h_zscore": 0.1,
            "liquidation_cascade_risk_usd": 1e4,
            "exchange_stress_score": 0.15,
            "exchange_netflow_7d_zscore": 0.0,
            "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0,
            "staking_unlock_risk": 0.0,
            "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0,
            "stablecoin_reserve_ratio": 0.5,
            "network_activity_score": 0.0,
            "confidence": 0.70,
            "timestamp": float(int(time.time())),
        }
        good = self._make_exchange_provider("good", good_metrics)

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[failing, good],
            macro_providers=[],
        )
        result = await agg.fetch_metrics()
        # Good provider's exchange_stress_score should appear
        assert result["exchange_stress_score"] == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_paid_gated_fields_reduce_confidence(self):
        """All paid fields at neutral → confidence penalised 5x vs zero paid missing."""
        from src.intelligence.providers.aggregator import (
            _CONFIDENCE_PENALTY_PER_MISSING,
            _PAID_GATED_FIELDS,
            MultiProviderIntelligenceAggregator,
        )

        # Provider with all paid fields at neutral (default GAP-015 state)
        neutral_metrics = {
            "binance_funding_rate_pct": 0.01,
            "futures_oi_change_pct": 1.0,
            "cross_exchange_basis_spread_bps": 5.0,
            "whale_buy_sell_ratio": 1.2,
            "liquidation_pressure_24h_zscore": 0.3,
            "liquidation_cascade_risk_usd": 1e5,
            "exchange_stress_score": 0.2,
            "exchange_netflow_7d_zscore": 0.0,  # neutral
            "exchange_reserve_ratio": 0.5,  # neutral
            "miner_netflow_signal": 0.0,  # neutral
            "staking_unlock_risk": 0.0,  # neutral
            "entity_exchange_imbalance": 0.0,  # neutral
            "btc_dominance_regime": 0.0,
            "stablecoin_reserve_ratio": 0.5,
            "network_activity_score": 0.0,
            "confidence": 0.80,
            "timestamp": float(int(time.time())),
        }
        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[self._make_exchange_provider("binance", neutral_metrics)],
            macro_providers=[],
        )
        result = await agg.fetch_metrics()
        # Expected: base_conf=0.80, minus 5 paid-missing x 0.05 = 0.55
        expected_conf = max(0.0, 0.80 - len(_PAID_GATED_FIELDS) * _CONFIDENCE_PENALTY_PER_MISSING)
        assert result["confidence"] == pytest.approx(expected_conf, abs=1e-6)


# ---------------------------------------------------------------------------
# 6. _inject_intelligence_features
# ---------------------------------------------------------------------------


class TestInjectIntelligenceFeatures:
    def test_finite_values_injected(self, base_feature_vec, full_intel_metrics):
        from src.features.pipeline import _inject_intelligence_features

        result = _inject_intelligence_features(base_feature_vec, full_intel_metrics)

        # At least the real free-field columns must be present
        free_fields = [
            "intelligence_binance_funding_rate_pct",
            "intelligence_exchange_stress_score",
            "intelligence_whale_buy_sell_ratio",
            "intelligence_futures_oi_change_pct",
            "intelligence_cross_exchange_basis_spread_bps",
            "intelligence_btc_dominance_regime",
            "intelligence_network_activity_score",
        ]
        for col in free_fields:
            assert col in result.index, f"Expected column missing: {col}"

    def test_nan_values_not_injected(self, base_feature_vec):
        from src.features.pipeline import _inject_intelligence_features

        metrics_with_nan = {
            "exchange_stress_score": float("nan"),
            "whale_buy_sell_ratio": 1.2,
            "confidence": 0.7,
        }
        result = _inject_intelligence_features(base_feature_vec, metrics_with_nan)
        # NaN field must NOT appear in result
        assert "intelligence_exchange_stress_score" not in result.index
        # Finite field must appear
        assert "intelligence_whale_buy_sell_ratio" in result.index

    def test_inf_values_not_injected(self, base_feature_vec):
        from src.features.pipeline import _inject_intelligence_features

        metrics = {"exchange_stress_score": float("inf"), "confidence": 0.8}
        result = _inject_intelligence_features(base_feature_vec, metrics)
        assert "intelligence_exchange_stress_score" not in result.index

    def test_base_features_preserved(self, base_feature_vec, full_intel_metrics):
        from src.features.pipeline import FEATURE_COLUMNS, _inject_intelligence_features

        result = _inject_intelligence_features(base_feature_vec, full_intel_metrics)
        for col in FEATURE_COLUMNS:
            assert col in result.index
            assert result[col] == pytest.approx(base_feature_vec[col])

    def test_confidence_included(self, base_feature_vec, full_intel_metrics):
        from src.features.pipeline import _inject_intelligence_features

        result = _inject_intelligence_features(base_feature_vec, full_intel_metrics)
        assert "intelligence_confidence" in result.index
        assert result["intelligence_confidence"] == pytest.approx(0.75)

    def test_empty_metrics_returns_base_unchanged(self, base_feature_vec):
        from src.features.pipeline import _inject_intelligence_features

        result = _inject_intelligence_features(base_feature_vec, {})
        pd.testing.assert_series_equal(result, base_feature_vec)

    def test_non_numeric_field_value_skipped(self, base_feature_vec):
        """A field that can't convert to float (e.g. a malformed provider
        response) must be dropped, not raise."""
        from src.features.pipeline import _inject_intelligence_features

        metrics = {"exchange_stress_score": "not-a-number", "confidence": 0.7}
        result = _inject_intelligence_features(base_feature_vec, metrics)
        assert "intelligence_exchange_stress_score" not in result.index

    def test_infinite_confidence_not_injected(self, base_feature_vec):
        from src.features.pipeline import _inject_intelligence_features

        metrics = {"exchange_stress_score": 0.5, "confidence": float("inf")}
        result = _inject_intelligence_features(base_feature_vec, metrics)
        assert "intelligence_confidence" not in result.index
        assert "intelligence_exchange_stress_score" in result.index

    def test_non_numeric_confidence_skipped(self, base_feature_vec):
        from src.features.pipeline import _inject_intelligence_features

        metrics = {"exchange_stress_score": 0.5, "confidence": "not-a-number"}
        result = _inject_intelligence_features(base_feature_vec, metrics)
        assert "intelligence_confidence" not in result.index
        assert "intelligence_exchange_stress_score" in result.index


# ---------------------------------------------------------------------------
# 7. build_inference_features with intelligence_metrics kwarg
# ---------------------------------------------------------------------------


class TestBuildInferenceFeaturesWithIntelligence:
    def _make_bars(self, n: int = 220) -> pd.DataFrame:
        """Minimal OHLCV DataFrame with enough rows for all feature windows (need ≥200)."""
        rng = np.random.default_rng(42)
        close = 30_000.0 + np.cumsum(rng.normal(0, 50, n))
        return pd.DataFrame(
            {
                "open": close - rng.uniform(0, 10, n),
                "high": close + rng.uniform(5, 20, n),
                "low": close - rng.uniform(5, 20, n),
                "close": close,
                "volume": rng.uniform(1e6, 5e6, n),
            }
        )

    def test_base_features_returned_when_no_intel(self):
        from src.features.pipeline import FEATURE_COLUMNS, build_inference_features

        bars = self._make_bars()
        vec = build_inference_features(bars)
        assert vec is not None
        assert set(FEATURE_COLUMNS).issubset(set(vec.index))

    def test_intel_features_appended_when_metrics_supplied(self, full_intel_metrics):
        from src.features.pipeline import FEATURE_COLUMNS, build_inference_features

        bars = self._make_bars()
        vec = build_inference_features(bars, intelligence_metrics=full_intel_metrics)
        assert vec is not None
        assert set(FEATURE_COLUMNS).issubset(set(vec.index))
        # At least one intelligence column should be present
        intel_cols = [c for c in vec.index if c.startswith("intelligence_")]
        assert len(intel_cols) > 0

    def test_base_only_when_metrics_empty(self):
        from src.features.pipeline import build_inference_features

        bars = self._make_bars()
        vec = build_inference_features(bars, intelligence_metrics={})
        assert vec is not None
        intel_cols = [c for c in vec.index if c.startswith("intelligence_")]
        assert len(intel_cols) == 0

    def test_nan_intel_fields_not_in_result(self):
        from src.features.pipeline import build_inference_features

        bars = self._make_bars()
        metrics_with_nan = {
            "exchange_stress_score": float("nan"),
            "whale_buy_sell_ratio": 1.3,
            "confidence": 0.7,
        }
        vec = build_inference_features(bars, intelligence_metrics=metrics_with_nan)
        assert vec is not None
        assert "intelligence_exchange_stress_score" not in vec.index
        assert "intelligence_whale_buy_sell_ratio" in vec.index

    def test_fast_path_also_injects_intelligence(self, full_intel_metrics):
        """Fast path (feature_matrix supplied) must also inject intelligence features."""
        from src.features.pipeline import (
            build_feature_matrix,
            build_inference_features,
        )

        bars = self._make_bars()
        fm = build_feature_matrix(bars)
        vec = build_inference_features(
            bars, feature_matrix=fm, intelligence_metrics=full_intel_metrics
        )
        assert vec is not None
        intel_cols = [c for c in vec.index if c.startswith("intelligence_")]
        assert len(intel_cols) > 0


# ---------------------------------------------------------------------------
# 8. Singleton integrity
# ---------------------------------------------------------------------------


class TestGetMultiProviderAggregatorSingleton:
    def test_same_instance_on_repeated_calls(self):
        import src.intelligence.providers.aggregator as agg_mod

        # Reset singleton for test isolation
        agg_mod._aggregator = None
        from src.intelligence.providers.aggregator import get_multi_provider_aggregator

        a1 = get_multi_provider_aggregator()
        a2 = get_multi_provider_aggregator()
        assert a1 is a2
        # cleanup
        agg_mod._aggregator = None

    def test_aggregator_has_all_five_providers(self):
        import src.intelligence.providers.aggregator as agg_mod

        agg_mod._aggregator = None
        from src.intelligence.providers.aggregator import get_multi_provider_aggregator

        agg = get_multi_provider_aggregator()
        ids = {p.exchange_id for p in agg._all_providers}
        assert "binance" in ids
        assert "okx" in ids
        assert "bybit" in ids
        assert "coingecko" in ids
        assert "blockchain_info" in ids
        agg_mod._aggregator = None


class TestGetOnChainAwareAggregatorSingleton:
    """OCI-008/012: get_onchain_aware_aggregator singleton and wiring."""

    def _mock_intel_cfg(self):
        """Return a MagicMock IntelligenceSettings with empty API keys (fail-open)."""
        cfg = MagicMock()
        cfg.arkham_api_key = ""
        cfg.arkham_cache_ttl_s = 60
        cfg.dune_api_key = ""
        cfg.dune_cache_ttl_s = 3600
        cfg.cryptoquant_api_key = ""
        cfg.coinglass_api_key = ""
        cfg.coinglass_cache_ttl_s = 30
        return cfg

    def test_same_instance_on_repeated_calls(self):
        import src.intelligence.providers.aggregator as agg_mod

        agg_mod._onchain_aware_aggregator = None
        from src.intelligence.providers.aggregator import get_onchain_aware_aggregator

        mock_settings = MagicMock()
        mock_settings.intelligence = self._mock_intel_cfg()
        with patch("src.config.get_settings", return_value=mock_settings):
            a1 = get_onchain_aware_aggregator()
            a2 = get_onchain_aware_aggregator()
        assert a1 is a2
        agg_mod._onchain_aware_aggregator = None

    def test_aggregator_has_exchange_macro_and_onchain_providers(self):
        import src.intelligence.providers.aggregator as agg_mod

        agg_mod._onchain_aware_aggregator = None
        from src.intelligence.providers.aggregator import (
            OnChainAwareAggregator,
            get_onchain_aware_aggregator,
        )

        mock_settings = MagicMock()
        mock_settings.intelligence = self._mock_intel_cfg()
        with patch("src.config.get_settings", return_value=mock_settings):
            agg = get_onchain_aware_aggregator()
        assert isinstance(agg, OnChainAwareAggregator)
        # Exchange + macro providers
        base_ids = {p.exchange_id for p in agg._all_providers}
        assert "binance" in base_ids
        assert "okx" in base_ids
        assert "bybit" in base_ids
        assert "coingecko" in base_ids
        assert "blockchain_info" in base_ids
        # On-chain providers
        oc_ids = {p.exchange_id for p in agg._onchain_providers}
        assert len(oc_ids) == 5
        assert "arkham_intel" in oc_ids
        assert "defillama" in oc_ids
        assert "dune_analytics" in oc_ids
        assert "cryptoquant" in oc_ids
        assert "coinglass" in oc_ids
        agg_mod._onchain_aware_aggregator = None

    def test_signal_engine_uses_onchain_aware_aggregator(self):
        """signal_engine._get_intel_aggregator must resolve to get_onchain_aware_aggregator."""
        import src.engine.signal_engine as se_mod
        from src.intelligence.providers.aggregator import get_onchain_aware_aggregator

        assert se_mod._get_intel_aggregator is get_onchain_aware_aggregator
