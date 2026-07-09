"""
OCI-007 — on-chain schema validation and merge tests.
"""
from __future__ import annotations
import math
import pytest
from src.intelligence.onchain.schema import (
    ONCHAIN_NEUTRAL,
    GATED_FIELDS,
    ALL_FIELDS,
    validate_provider_result,
    merge_onchain_results,
)


class TestONCHAIN_NEUTRAL:
    def test_all_floats(self):
        for k, v in ONCHAIN_NEUTRAL.items():
            assert isinstance(v, float), f"{k} not float"

    def test_confidence_zero(self):
        assert ONCHAIN_NEUTRAL["confidence"] == 0.0

    def test_whale_ratio_neutral(self):
        assert ONCHAIN_NEUTRAL["whale_buy_sell_ratio"] == 1.0


class TestValidateProviderResult:
    def _result(self, **overrides):
        r = dict(ONCHAIN_NEUTRAL)
        r.update(overrides)
        return r

    def test_clean_passthrough(self):
        r = self._result(confidence=0.9)
        out = validate_provider_result(r, "test")
        assert out["confidence"] == pytest.approx(0.9)

    def test_nan_replaced_by_neutral(self):
        r = self._result(exchange_reserve_ratio=float("nan"))
        out = validate_provider_result(r, "test")
        assert out["exchange_reserve_ratio"] == 0.5

    def test_inf_replaced_by_neutral(self):
        r = self._result(exchange_netflow_7d_zscore=float("inf"))
        out = validate_provider_result(r, "test")
        assert out["exchange_netflow_7d_zscore"] == 0.0

    def test_confidence_clamped_high(self):
        r = self._result(confidence=2.5)
        out = validate_provider_result(r, "test")
        assert out["confidence"] == pytest.approx(1.0)

    def test_confidence_clamped_low(self):
        r = self._result(confidence=-0.5)
        out = validate_provider_result(r, "test")
        assert out["confidence"] == pytest.approx(0.0)

    def test_internal_field_stripped(self):
        r = self._result(confidence=1.0)
        r["exchange_stress_score_mvrv_contrib"] = 0.3
        out = validate_provider_result(r, "test")
        assert "exchange_stress_score_mvrv_contrib" not in out

    def test_strict_raises_on_missing_required(self):
        with pytest.raises(ValueError, match="missing required field"):
            validate_provider_result({}, "test", strict=True)

    def test_non_strict_fills_neutral(self):
        out = validate_provider_result({}, "test", strict=False)
        assert out["confidence"] == 0.0


class TestMergeOnchainResults:
    def _base(self, confidence=1.0, **overrides):
        r = dict(ONCHAIN_NEUTRAL)
        r["confidence"] = confidence
        r["timestamp"] = 1_700_000_000.0
        r.update(overrides)
        return r

    def test_empty_returns_neutral(self):
        out = merge_onchain_results([])
        for k, v in ONCHAIN_NEUTRAL.items():
            assert out[k] == pytest.approx(v), k

    def test_single_provider_passthrough(self):
        r = self._base(exchange_netflow_7d_zscore=1.5)
        out = merge_onchain_results([r])
        assert out["exchange_netflow_7d_zscore"] == pytest.approx(1.5)

    def test_two_providers_weighted_mean(self):
        r1 = self._base(confidence=1.0, exchange_netflow_7d_zscore=2.0)
        r2 = self._base(confidence=0.5, exchange_netflow_7d_zscore=1.0)
        out = merge_onchain_results([r1, r2])
        # weighted: (2.0*1.0 + 1.0*0.5) / (1.0+0.5) = 2.5/1.5 = 1.667
        assert out["exchange_netflow_7d_zscore"] == pytest.approx(2.5 / 1.5)

    def test_neutral_fields_stay_neutral(self):
        r1 = self._base(confidence=1.0)  # all neutral
        out = merge_onchain_results([r1])
        assert out["exchange_netflow_7d_zscore"] == 0.0

    def test_mvrv_contrib_added_to_stress_score(self):
        r = self._base(confidence=1.0, exchange_stress_score=0.2)
        r["exchange_stress_score_mvrv_contrib"] = 0.3
        out = merge_onchain_results([r])
        # 0.2 (non-neutral) is averaged; 0.3 added additively
        assert out["exchange_stress_score"] > 0.2

    def test_confidence_averaged(self):
        r1 = self._base(confidence=0.8)
        r2 = self._base(confidence=0.4)
        out = merge_onchain_results([r1, r2])
        assert out["confidence"] == pytest.approx(0.6)

    def test_timestamp_is_max(self):
        r1 = self._base(confidence=1.0)
        r1["timestamp"] = 1_000.0
        r2 = self._base(confidence=1.0)
        r2["timestamp"] = 2_000.0
        out = merge_onchain_results([r1, r2])
        assert out["timestamp"] == pytest.approx(2_000.0)

    def test_stress_clamped_at_one(self):
        r = self._base(confidence=1.0, exchange_stress_score=0.9)
        r["exchange_stress_score_mvrv_contrib"] = 0.5
        out = merge_onchain_results([r])
        assert out["exchange_stress_score"] <= 1.0


class TestNewSchemaFields:
    """OCI-012: defi_tvl_7d_change_pct / mvrv_z_score / sopr coverage."""

    def test_new_fields_in_onchain_neutral(self):
        assert "defi_tvl_7d_change_pct" in ONCHAIN_NEUTRAL
        assert "mvrv_z_score" in ONCHAIN_NEUTRAL
        assert "sopr" in ONCHAIN_NEUTRAL

    def test_new_field_neutral_defaults(self):
        assert ONCHAIN_NEUTRAL["defi_tvl_7d_change_pct"] == 0.0
        assert ONCHAIN_NEUTRAL["mvrv_z_score"] == 0.0
        assert ONCHAIN_NEUTRAL["sopr"] == 0.0

    def test_dune_fields_are_gated(self):
        assert "mvrv_z_score" in GATED_FIELDS
        assert "sopr" in GATED_FIELDS

    def test_defi_tvl_not_gated(self):
        # DefiLlama is public; defi_tvl_7d_change_pct should pass without a key
        assert "defi_tvl_7d_change_pct" not in GATED_FIELDS

    def test_new_fields_in_all_fields(self):
        assert "defi_tvl_7d_change_pct" in ALL_FIELDS
        assert "mvrv_z_score" in ALL_FIELDS
        assert "sopr" in ALL_FIELDS

    def test_validate_passes_new_fields(self):
        r = dict(ONCHAIN_NEUTRAL)
        r.update({"confidence": 0.8, "defi_tvl_7d_change_pct": -3.5,
                   "mvrv_z_score": 2.1, "sopr": 0.4, "timestamp": 1.0})
        out = validate_provider_result(r, "test")
        assert out["defi_tvl_7d_change_pct"] == pytest.approx(-3.5)
        assert out["mvrv_z_score"] == pytest.approx(2.1)
        assert out["sopr"] == pytest.approx(0.4)
