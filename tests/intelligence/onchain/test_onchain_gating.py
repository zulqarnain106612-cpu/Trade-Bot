"""
OCI-010 — On-chain field gating policy tests.

Verifies that GATED_FIELDS remain at neutral when no API key is provisioned,
and become active once a provider with confidence > 0 is present.
"""
from __future__ import annotations
import pytest
from src.intelligence.onchain.schema import (
    ONCHAIN_NEUTRAL,
    GATED_FIELDS,
    merge_onchain_results,
    validate_provider_result,
)
from src.intelligence.onchain.cryptoquant_provider import CryptoQuantProvider
from src.intelligence.onchain.coinglass_provider import CoinglassProvider
from src.intelligence.onchain.dune_provider import DuneProvider
from src.intelligence.onchain.arkham_provider import ArkhamProvider


class TestGatedFieldsDefinition:
    def test_gated_fields_are_subset_of_all_fields(self):
        from src.intelligence.onchain.schema import ALL_FIELDS
        assert GATED_FIELDS.issubset(ALL_FIELDS)

    def test_gated_fields_have_neutral_values(self):
        for f in GATED_FIELDS:
            assert f in ONCHAIN_NEUTRAL, f"{f} not in ONCHAIN_NEUTRAL"

    def test_expected_gated_fields(self):
        expected = {
            "exchange_netflow_7d_zscore",
            "exchange_reserve_ratio",
            "miner_netflow_signal",
            "staking_unlock_risk",
            "entity_exchange_imbalance",
            # Dune Analytics (paid key required) — OCI-012
            "mvrv_z_score",
            "sopr",
        }
        assert GATED_FIELDS == expected


class TestGatingViaDisabledProviders:
    @pytest.mark.asyncio
    async def test_cryptoquant_disabled_gated_neutral(self):
        p = CryptoQuantProvider(api_key="")
        m = await p.fetch_metrics()
        assert m["exchange_netflow_7d_zscore"] == pytest.approx(0.0)
        assert m["exchange_reserve_ratio"] == pytest.approx(0.5)
        assert m["miner_netflow_signal"] == pytest.approx(0.0)
        assert m["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_coinglass_disabled_non_gated_neutral(self):
        p = CoinglassProvider(api_key="")
        m = await p.fetch_metrics()
        # Coinglass fields are not gated but should still be neutral
        assert m["futures_oi_change_pct"] == pytest.approx(0.0)
        assert m["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_dune_disabled_gated_neutral(self):
        p = DuneProvider(api_key="")
        m = await p.fetch_metrics()
        assert m["miner_netflow_signal"] == pytest.approx(0.0)
        assert m["mvrv_z_score"] == pytest.approx(0.0)
        assert m["sopr"] == pytest.approx(0.0)
        assert m["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_arkham_disabled_gated_neutral(self):
        p = ArkhamProvider(api_key="")
        m = await p.fetch_metrics()
        assert m["exchange_reserve_ratio"] == pytest.approx(0.5)
        assert m["confidence"] == 0.0


class TestMergeGatingPolicy:
    def _disabled_result(self, **extra):
        r = dict(ONCHAIN_NEUTRAL)
        r["confidence"] = 0.0
        r["timestamp"] = 1_700_000_000.0
        r.update(extra)
        return r

    def _enabled_result(self, **extra):
        r = dict(ONCHAIN_NEUTRAL)
        r["confidence"] = 0.9
        r["timestamp"] = 1_700_000_001.0
        r.update(extra)
        return r

    def test_disabled_only_gated_stay_neutral(self):
        r = self._disabled_result(exchange_netflow_7d_zscore=2.5)
        out = merge_onchain_results([r])
        # confidence=0 → non-neutral value is still present in merge
        # (merge itself doesn't gate; gating is done in aggregator)
        # but confidence is 0 so aggregator gate blocks it
        assert out["confidence"] == pytest.approx(0.0)

    def test_enabled_gated_field_passes_merge(self):
        r = self._enabled_result(exchange_netflow_7d_zscore=2.5)
        out = merge_onchain_results([r])
        assert out["exchange_netflow_7d_zscore"] == pytest.approx(2.5)
        assert out["confidence"] > 0.0

    def test_mix_disabled_enabled_confidence_averaged(self):
        r1 = self._disabled_result()
        r2 = self._enabled_result(exchange_netflow_7d_zscore=3.0)
        out = merge_onchain_results([r1, r2])
        # confidence = (0 + 0.9) / 2 = 0.45
        assert out["confidence"] == pytest.approx(0.45)
        # z-score blended from r2 (non-neutral)
        assert out["exchange_netflow_7d_zscore"] == pytest.approx(3.0)
