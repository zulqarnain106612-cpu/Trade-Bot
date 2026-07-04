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

import math
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
        "binance_funding_rate_pct":        0.01,
        "futures_oi_change_pct":           1.5,
        "cross_exchange_basis_spread_bps": 8.0,
        "whale_buy_sell_ratio":            1.3,
        "liquidation_pressure_24h_zscore": 0.5,
        "liquidation_cascade_risk_usd":    500_000.0,
        "exchange_stress_score":           0.25,
        "exchange_netflow_7d_zscore":      0.0,   # paid-gated: neutral
        "exchange_reserve_ratio":          0.5,   # paid-gated: neutral
        "miner_netflow_signal":            0.0,   # paid-gated: neutral
        "staking_unlock_risk":             0.0,   # paid-gated: neutral
        "entity_exchange_imbalance":       0.0,   # paid-gated: neutral
        "btc_dominance_regime":            0.8,
        "stablecoin_reserve_ratio":        0.07,
        "network_activity_score":          0.4,
        "confidence":                      0.75,
        "timestamp":                       float(int(time.time())),
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
            def exchange_id(self): return "test"
            async def initialize(self): pass
            # Missing close() and fetch_metrics()

        with pytest.raises(TypeError):
            Incomplete()

    def test_complete_subclass_instantiates(self):
        from src.intelligence.providers.base import ExchangeIntelligenceProvider

        class Complete(ExchangeIntelligenceProvider):
            @property
            def exchange_id(self): return "test"
            async def initialize(self): pass
            async def close(self): pass
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
        metrics_full_ok = await provider.fetch_metrics()
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

        async def counting_fetch():
            nonlocal call_count
            call_count += 1
            return {"rate_pct": 0.01, "zscore": 0.3}

        provider._fetch_funding_data = counting_fetch
        provider._fetch_oi_data = AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0})
        provider._fetch_basis_data = AsyncMock(return_value=0.0)
        provider._fetch_whale_taker_ratio = AsyncMock(return_value=1.0)

        await provider.fetch_metrics()
        await provider.fetch_metrics()  # second call — funding should be cached
        # call_count ≤ 2 because cache hits for OI/basis/whale; funding: 1 real call
        assert call_count == 1


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
            "market_cap_percentage": {"btc": 50.0, "usdt": 5.0, "usdc": 2.0, "dai": 0.5, "frax": 0.1},
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
    async def test_stablecoin_ratio_bounded(self, provider):
        async def fake_fetch():
            return {
                "market_cap_percentage": {"btc": 40.0, "usdt": 8.0, "usdc": 4.0, "dai": 1.0, "frax": 0.5},
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
                "market_cap_percentage": {"btc": 48.0, "usdt": 6.0, "usdc": 3.0, "dai": 0.5, "frax": 0.2},
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
            def exchange_id(self): return exchange_id
            async def initialize(self): pass
            async def close(self): pass
            async def fetch_metrics(self) -> dict[str, float]:
                return metrics

        return MockProvider()

    @pytest.mark.asyncio
    async def test_all_intel_fields_present_in_merged(self):
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

        binance_metrics = {
            "binance_funding_rate_pct": 0.01, "futures_oi_change_pct": 1.5,
            "cross_exchange_basis_spread_bps": 8.0, "whale_buy_sell_ratio": 1.3,
            "liquidation_pressure_24h_zscore": 0.5, "liquidation_cascade_risk_usd": 5e5,
            "exchange_stress_score": 0.25,
            "exchange_netflow_7d_zscore": 0.0, "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0, "staking_unlock_risk": 0.0, "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0, "stablecoin_reserve_ratio": 0.5, "network_activity_score": 0.0,
            "confidence": 0.75, "timestamp": float(int(time.time())),
        }
        macro_metrics = {
            **{k: 0.0 for k in binance_metrics},
            "whale_buy_sell_ratio": 1.0, "exchange_reserve_ratio": 0.5, "stablecoin_reserve_ratio": 0.5,
            "btc_dominance_regime": 0.9, "stablecoin_reserve_ratio": 0.08, "network_activity_score": 0.4,
            "confidence": 0.90, "timestamp": float(int(time.time())),
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
                "binance_funding_rate_pct": 0.0, "futures_oi_change_pct": 0.0,
                "cross_exchange_basis_spread_bps": 0.0, "whale_buy_sell_ratio": 1.0,
                "liquidation_pressure_24h_zscore": 0.0, "liquidation_cascade_risk_usd": 0.0,
                "exchange_stress_score": 0.0, "exchange_netflow_7d_zscore": 0.0,
                "exchange_reserve_ratio": 0.5, "miner_netflow_signal": 0.0,
                "staking_unlock_risk": 0.0, "entity_exchange_imbalance": 0.0,
                "btc_dominance_regime": btc_dom,
                "stablecoin_reserve_ratio": 0.5, "network_activity_score": 0.0,
                "confidence": conf, "timestamp": float(int(time.time())),
            }

        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[self._make_exchange_provider("binance", _base("binance", 0.0, 0.75))],
            macro_providers=[self._make_exchange_provider("coingecko", _base("coingecko", 1.5, 0.90))],
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
                "binance_funding_rate_pct": 0.01, "futures_oi_change_pct": 1.0,
                "cross_exchange_basis_spread_bps": 5.0, "whale_buy_sell_ratio": 1.2,
                "liquidation_pressure_24h_zscore": 0.3, "liquidation_cascade_risk_usd": 1e5,
                "exchange_stress_score": stress,
                "exchange_netflow_7d_zscore": 0.0, "exchange_reserve_ratio": 0.5,
                "miner_netflow_signal": 0.0, "staking_unlock_risk": 0.0, "entity_exchange_imbalance": 0.0,
                "btc_dominance_regime": 0.0, "stablecoin_reserve_ratio": 0.5, "network_activity_score": 0.0,
                "confidence": conf, "timestamp": float(int(time.time())),
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
            "binance_funding_rate_pct": 0.02, "futures_oi_change_pct": 0.5,
            "cross_exchange_basis_spread_bps": 3.0, "whale_buy_sell_ratio": 1.1,
            "liquidation_pressure_24h_zscore": 0.1, "liquidation_cascade_risk_usd": 1e4,
            "exchange_stress_score": 0.15,
            "exchange_netflow_7d_zscore": 0.0, "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0, "staking_unlock_risk": 0.0, "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0, "stablecoin_reserve_ratio": 0.5, "network_activity_score": 0.0,
            "confidence": 0.70, "timestamp": float(int(time.time())),
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
        """All paid fields at neutral → confidence penalised 5× vs zero paid missing."""
        from src.intelligence.providers.aggregator import MultiProviderIntelligenceAggregator, _PAID_GATED_FIELDS, _CONFIDENCE_PENALTY_PER_MISSING

        # Provider with all paid fields at neutral (default GAP-015 state)
        neutral_metrics = {
            "binance_funding_rate_pct": 0.01, "futures_oi_change_pct": 1.0,
            "cross_exchange_basis_spread_bps": 5.0, "whale_buy_sell_ratio": 1.2,
            "liquidation_pressure_24h_zscore": 0.3, "liquidation_cascade_risk_usd": 1e5,
            "exchange_stress_score": 0.2,
            "exchange_netflow_7d_zscore": 0.0,   # neutral
            "exchange_reserve_ratio": 0.5,         # neutral
            "miner_netflow_signal": 0.0,           # neutral
            "staking_unlock_risk": 0.0,            # neutral
            "entity_exchange_imbalance": 0.0,      # neutral
            "btc_dominance_regime": 0.0, "stablecoin_reserve_ratio": 0.5, "network_activity_score": 0.0,
            "confidence": 0.80, "timestamp": float(int(time.time())),
        }
        agg = MultiProviderIntelligenceAggregator(
            exchange_providers=[self._make_exchange_provider("binance", neutral_metrics)],
            macro_providers=[],
        )
        result = await agg.fetch_metrics()
        # Expected: base_conf=0.80, minus 5 paid-missing × 0.05 = 0.55
        expected_conf = max(0.0, 0.80 - len(_PAID_GATED_FIELDS) * _CONFIDENCE_PENALTY_PER_MISSING)
        assert result["confidence"] == pytest.approx(expected_conf, abs=1e-6)


# ---------------------------------------------------------------------------
# 6. _inject_intelligence_features
# ---------------------------------------------------------------------------

class TestInjectIntelligenceFeatures:
    def test_finite_values_injected(self, base_feature_vec, full_intel_metrics):
        from src.features.pipeline import _inject_intelligence_features
        from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS

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
        from src.features.pipeline import _inject_intelligence_features
        from src.features.pipeline import FEATURE_COLUMNS

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


# ---------------------------------------------------------------------------
# 7. build_inference_features with intelligence_metrics kwarg
# ---------------------------------------------------------------------------

class TestBuildInferenceFeaturesWithIntelligence:
    def _make_bars(self, n: int = 120) -> pd.DataFrame:
        """Minimal OHLCV DataFrame with enough rows for all feature windows."""
        rng = np.random.default_rng(42)
        close = 30_000.0 + np.cumsum(rng.normal(0, 50, n))
        return pd.DataFrame(
            {
                "open":   close - rng.uniform(0, 10, n),
                "high":   close + rng.uniform(5, 20, n),
                "low":    close - rng.uniform(5, 20, n),
                "close":  close,
                "volume": rng.uniform(1e6, 5e6, n),
            }
        )

    def test_base_features_returned_when_no_intel(self):
        from src.features.pipeline import build_inference_features, FEATURE_COLUMNS
        bars = self._make_bars()
        vec = build_inference_features(bars)
        assert vec is not None
        assert set(FEATURE_COLUMNS).issubset(set(vec.index))

    def test_intel_features_appended_when_metrics_supplied(self, full_intel_metrics):
        from src.features.pipeline import build_inference_features, FEATURE_COLUMNS
        bars = self._make_bars()
        vec = build_inference_features(bars, intelligence_metrics=full_intel_metrics)
        assert vec is not None
        assert set(FEATURE_COLUMNS).issubset(set(vec.index))
        # At least one intelligence column should be present
        intel_cols = [c for c in vec.index if c.startswith("intelligence_")]
        assert len(intel_cols) > 0

    def test_base_only_when_metrics_empty(self):
        from src.features.pipeline import build_inference_features, FEATURE_COLUMNS
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
        from src.features.pipeline import build_inference_features, build_feature_matrix, FEATURE_COLUMNS
        bars = self._make_bars()
        fm = build_feature_matrix(bars)
        vec = build_inference_features(bars, feature_matrix=fm, intelligence_metrics=full_intel_metrics)
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

    def test_aggregator_has_all_four_providers(self):
        import src.intelligence.providers.aggregator as agg_mod
        agg_mod._aggregator = None
        from src.intelligence.providers.aggregator import get_multi_provider_aggregator
        from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider
        from src.intelligence.providers.okx_provider import OKXIntelligenceProvider
        from src.intelligence.providers.coingecko_provider import CoinGeckoIntelligenceProvider
        from src.intelligence.providers.blockchain_provider import BlockchainIntelligenceProvider

        agg = get_multi_provider_aggregator()
        ids = {p.exchange_id for p in agg._all_providers}
        assert "binance" in ids
        assert "okx" in ids
        assert "coingecko" in ids
        assert "blockchain_info" in ids
        agg_mod._aggregator = None
