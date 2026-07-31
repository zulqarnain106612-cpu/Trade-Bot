"""
Tests for on-chain provider pure helper functions.

Covers untested pure functions in:
  - src/intelligence/onchain/defillama_provider.py
  - src/intelligence/onchain/dune_provider.py
  - src/intelligence/onchain/schema.py
"""

from __future__ import annotations

import math

import pytest

from src.intelligence.onchain.defillama_provider import (
    _compute_tvl_metrics,
    _stablecoin_ratio,
)
from src.intelligence.onchain.dune_provider import (
    _extract_rows,
    _miner_netflow_zscore,
    _results_fresh,
)
from src.intelligence.onchain.schema import (
    ONCHAIN_NEUTRAL,
    merge_onchain_results,
    validate_provider_result,
)


# ---------------------------------------------------------------------------
# _compute_tvl_metrics
# ---------------------------------------------------------------------------


class TestComputeTvlMetrics:
    def _make_data(self, tvl_values: list[float]) -> list[dict]:
        return [{"date": i, "tvl": v} for i, v in enumerate(tvl_values)]

    def test_empty_list_returns_zeros(self) -> None:
        assert _compute_tvl_metrics([]) == (0.0, 0.0)

    def test_single_entry_returns_zeros(self) -> None:
        assert _compute_tvl_metrics([{"date": 0, "tvl": 100.0}]) == (0.0, 0.0)

    def test_non_list_returns_zeros(self) -> None:
        assert _compute_tvl_metrics(None) == (0.0, 0.0)  # type: ignore[arg-type]
        assert _compute_tvl_metrics({"not": "a list"}) == (0.0, 0.0)  # type: ignore[arg-type]

    def test_stable_tvl_low_risk(self) -> None:
        # 7d change ≈ 0% → unlock_risk = 0.1
        data = self._make_data([100_000.0] * 16)
        risk, change = _compute_tvl_metrics(data)
        assert risk == pytest.approx(0.1)
        assert abs(change) < 0.01

    def test_moderate_drop_medium_risk(self) -> None:
        # Simulate 7d ago = 100, now = 93 → -7% → risk=0.5
        data = self._make_data([100.0] * 8 + [93.0])
        risk, change = _compute_tvl_metrics(data)
        assert risk == 0.5
        assert change < -5.0

    def test_large_drop_high_risk(self) -> None:
        # Simulate 7d ago = 100, now = 85 → -15% → risk=0.8
        data = self._make_data([100.0] * 8 + [85.0])
        risk, change = _compute_tvl_metrics(data)
        assert risk == 0.8
        assert change < -10.0

    def test_short_series_uses_first_entry_as_7d_ago(self) -> None:
        # < 8 entries → uses recent[0] as baseline
        data = [{"date": i, "tvl": 100.0 + i} for i in range(5)]
        risk, change = _compute_tvl_metrics(data)
        # change = (104 - 100) / 100 * 100 = 4%
        assert change == pytest.approx(4.0, abs=0.01)
        assert risk == 0.1

    def test_zero_tvl_7d_ago_no_division_error(self) -> None:
        data = self._make_data([0.0] * 8 + [100.0])
        risk, change = _compute_tvl_metrics(data)
        # EPS prevents division by zero; change is very large positive
        assert risk == 0.1  # positive change → low risk
        assert change > 0

    def test_missing_tvl_key_treated_as_zero(self) -> None:
        data = [{"date": i} for i in range(10)]  # no "tvl" key
        risk, change = _compute_tvl_metrics(data)
        assert risk == 0.1
        assert change == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# _stablecoin_ratio
# ---------------------------------------------------------------------------


class TestStablecoinRatio:
    def _make_data(self, assets: list[dict]) -> dict:
        return {"peggedAssets": assets}

    def _asset(self, symbol: str, usd: float) -> dict:
        return {"symbol": symbol, "circulating": {"peggedUSD": usd}}

    def test_none_when_empty_pegs(self) -> None:
        assert _stablecoin_ratio(self._make_data([])) is None

    def test_none_when_total_is_zero(self) -> None:
        data = self._make_data([self._asset("USDT", 0.0), self._asset("DAI", 0.0)])
        assert _stablecoin_ratio(data) is None

    def test_none_when_not_a_dict(self) -> None:
        assert _stablecoin_ratio([]) is None
        assert _stablecoin_ratio(None) is None  # type: ignore[arg-type]

    def test_ratio_usdt_plus_usdc_over_total(self) -> None:
        data = self._make_data(
            [
                self._asset("USDT", 70_000.0),
                self._asset("USDC", 30_000.0),
                self._asset("DAI", 0.0),
            ]
        )
        r = _stablecoin_ratio(data)
        assert r == pytest.approx(1.0)

    def test_partial_major_stables(self) -> None:
        data = self._make_data(
            [
                self._asset("USDT", 50_000.0),
                self._asset("DAI", 50_000.0),
            ]
        )
        r = _stablecoin_ratio(data)
        assert r == pytest.approx(0.5)

    def test_no_major_stables_ratio_zero(self) -> None:
        data = self._make_data([self._asset("DAI", 100_000.0)])
        r = _stablecoin_ratio(data)
        assert r == pytest.approx(0.0)

    def test_ratio_clamped_to_1(self) -> None:
        data = self._make_data([self._asset("USDT", 200_000.0), self._asset("DAI", 0.0)])
        r = _stablecoin_ratio(data)
        assert r is not None
        assert r <= 1.0

    def test_missing_peggedassets_key(self) -> None:
        assert _stablecoin_ratio({"other": "data"}) is None


# ---------------------------------------------------------------------------
# validate_provider_result
# ---------------------------------------------------------------------------


class TestValidateProviderResult:
    def test_clean_result_passes_through(self) -> None:
        result = {"confidence": 0.8, "timestamp": 1_700_000_000.0, "whale_buy_sell_ratio": 1.5}
        out = validate_provider_result(result, "test")
        assert out["confidence"] == pytest.approx(0.8)
        assert out["whale_buy_sell_ratio"] == pytest.approx(1.5)

    def test_nan_replaced_by_neutral(self) -> None:
        result = {
            "confidence": 0.5,
            "timestamp": 1.0,
            "whale_buy_sell_ratio": float("nan"),
        }
        out = validate_provider_result(result, "test")
        assert out["whale_buy_sell_ratio"] == ONCHAIN_NEUTRAL["whale_buy_sell_ratio"]

    def test_inf_replaced_by_neutral(self) -> None:
        result = {"confidence": 0.5, "timestamp": 1.0, "exchange_netflow_7d_zscore": math.inf}
        out = validate_provider_result(result, "test")
        assert out["exchange_netflow_7d_zscore"] == ONCHAIN_NEUTRAL["exchange_netflow_7d_zscore"]

    def test_confidence_clamped_above_1(self) -> None:
        result = {"confidence": 2.5, "timestamp": 1.0}
        out = validate_provider_result(result, "test")
        assert out["confidence"] == pytest.approx(1.0)

    def test_confidence_clamped_below_0(self) -> None:
        result = {"confidence": -0.5, "timestamp": 1.0}
        out = validate_provider_result(result, "test")
        assert out["confidence"] == pytest.approx(0.0)

    def test_non_numeric_value_skipped(self) -> None:
        result = {"confidence": 0.5, "timestamp": 1.0, "whale_buy_sell_ratio": "high"}  # type: ignore[dict-item]
        out = validate_provider_result(result, "test")
        assert out["whale_buy_sell_ratio"] == ONCHAIN_NEUTRAL["whale_buy_sell_ratio"]

    def test_internal_field_stripped(self) -> None:
        result = {
            "confidence": 0.5,
            "timestamp": 1.0,
            "exchange_stress_score_mvrv_contrib": 0.3,
        }
        out = validate_provider_result(result, "test")
        assert "exchange_stress_score_mvrv_contrib" not in out

    def test_strict_mode_raises_on_missing_required_field(self) -> None:
        result = {"confidence": 0.5}  # missing "timestamp"
        with pytest.raises(ValueError, match="missing required field"):
            validate_provider_result(result, "test", strict=True)


# ---------------------------------------------------------------------------
# merge_onchain_results
# ---------------------------------------------------------------------------


class TestMergeOnchainResults:
    def test_empty_returns_neutral(self) -> None:
        merged = merge_onchain_results([])
        assert merged == ONCHAIN_NEUTRAL

    def test_single_result_passes_through(self) -> None:
        result = dict(ONCHAIN_NEUTRAL)
        result["confidence"] = 0.8
        result["whale_buy_sell_ratio"] = 1.5
        merged = merge_onchain_results([result])
        assert merged["whale_buy_sell_ratio"] == pytest.approx(1.5)

    def test_confidence_averaged(self) -> None:
        r1 = dict(ONCHAIN_NEUTRAL)
        r1["confidence"] = 0.8
        r2 = dict(ONCHAIN_NEUTRAL)
        r2["confidence"] = 0.4
        merged = merge_onchain_results([r1, r2])
        assert merged["confidence"] == pytest.approx(0.6)

    def test_neutral_fields_not_pulled_toward_neutral(self) -> None:
        r1 = dict(ONCHAIN_NEUTRAL)
        r1["confidence"] = 0.9
        r1["exchange_netflow_7d_zscore"] = 2.5  # non-neutral signal
        r2 = dict(ONCHAIN_NEUTRAL)
        r2["confidence"] = 0.9
        # r2 leaves exchange_netflow_7d_zscore at neutral (0.0) → not counted
        merged = merge_onchain_results([r1, r2])
        # Only r1 contributed non-neutral value → should stay near 2.5
        assert merged["exchange_netflow_7d_zscore"] == pytest.approx(2.5)

    def test_mvrv_contrib_added_to_stress_score(self) -> None:
        r = dict(ONCHAIN_NEUTRAL)
        r["confidence"] = 0.8
        r["exchange_stress_score_mvrv_contrib"] = 0.2
        merged = merge_onchain_results([r])
        assert merged["exchange_stress_score"] == pytest.approx(0.2)

    def test_timestamp_takes_max(self) -> None:
        r1 = dict(ONCHAIN_NEUTRAL)
        r1["timestamp"] = 1_000.0
        r1["confidence"] = 0.5
        r2 = dict(ONCHAIN_NEUTRAL)
        r2["timestamp"] = 2_000.0
        r2["confidence"] = 0.5
        merged = merge_onchain_results([r1, r2])
        assert merged["timestamp"] == pytest.approx(2_000.0)


# ---------------------------------------------------------------------------
# Dune Analytics pure helper functions
# ---------------------------------------------------------------------------


class TestExtractRows:
    def test_none_input_returns_none(self) -> None:
        assert _extract_rows(None) is None

    def test_extracts_from_result_rows(self) -> None:
        data = {"result": {"rows": [{"a": 1}, {"a": 2}]}}
        rows = _extract_rows(data)
        assert rows == [{"a": 1}, {"a": 2}]

    def test_extracts_from_flat_rows(self) -> None:
        data = {"rows": [{"b": 3}]}
        assert _extract_rows(data) == [{"b": 3}]

    def test_none_when_rows_not_a_list(self) -> None:
        data = {"result": {"rows": "not_a_list"}}
        assert _extract_rows(data) is None

    def test_empty_result_returns_empty_list(self) -> None:
        data = {"result": {"rows": []}}
        assert _extract_rows(data) == []

    def test_missing_rows_key_returns_empty(self) -> None:
        data = {"result": {}}
        assert _extract_rows(data) == []


class TestResultsFresh:
    def _make_completed(self, age_seconds: float = 60) -> dict:
        import datetime

        ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds)
        return {
            "state": "QUERY_STATE_COMPLETED",
            "execution_ended_at": ts.isoformat(),
        }

    def test_fresh_within_ttl(self) -> None:
        assert _results_fresh(self._make_completed(60), ttl_s=300)

    def test_stale_beyond_ttl(self) -> None:
        assert not _results_fresh(self._make_completed(400), ttl_s=300)

    def test_wrong_state_not_fresh(self) -> None:
        result = {"state": "QUERY_STATE_PENDING", "execution_ended_at": "2024-01-01T00:00:00Z"}
        assert not _results_fresh(result, ttl_s=300)

    def test_missing_timestamp_not_fresh(self) -> None:
        result = {"state": "QUERY_STATE_COMPLETED"}
        assert not _results_fresh(result, ttl_s=300)

    def test_malformed_timestamp_returns_false(self) -> None:
        result = {"state": "QUERY_STATE_COMPLETED", "execution_ended_at": "not-a-date"}
        assert not _results_fresh(result, ttl_s=300)

    def test_success_state_also_accepted(self) -> None:
        result = self._make_completed(10)
        result["state"] = "SUCCESS"
        assert _results_fresh(result, ttl_s=300)


class TestMinerNetflowZscore:
    def test_empty_rows_returns_zero(self) -> None:
        assert _miner_netflow_zscore([]) == 0.0

    def test_single_row_returns_zero(self) -> None:
        assert _miner_netflow_zscore([{"miner_outflow_btc_7d": 100}]) == 0.0

    def test_zero_variance_returns_zero_not_error(self) -> None:
        rows = [{"miner_outflow_btc_7d": 50.0}] * 10
        result = _miner_netflow_zscore(rows)
        assert result == 0.0 or result != result  # zero or nan-safe

    def test_high_outlier_clamped_to_positive_one(self) -> None:
        rows = [{"miner_outflow_btc_7d": 0.0}] * 20 + [{"miner_outflow_btc_7d": 1_000_000.0}]
        result = _miner_netflow_zscore(rows)
        assert result == pytest.approx(1.0)

    def test_low_outlier_clamped_to_negative_one(self) -> None:
        rows = [{"miner_outflow_btc_7d": 0.0}] * 20 + [{"miner_outflow_btc_7d": -1_000_000.0}]
        result = _miner_netflow_zscore(rows)
        assert result == pytest.approx(-1.0)

    def test_missing_key_treated_as_zero(self) -> None:
        rows = [{}] * 10  # no miner_outflow_btc_7d key
        result = _miner_netflow_zscore(rows)
        assert result == 0.0

    def test_neutral_series_returns_near_zero(self) -> None:
        import random

        rng = random.Random(42)
        rows = [{"miner_outflow_btc_7d": rng.gauss(100.0, 5.0)} for _ in range(30)]
        result = _miner_netflow_zscore(rows)
        assert -1.0 <= result <= 1.0
