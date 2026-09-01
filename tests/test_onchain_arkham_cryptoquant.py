"""Tests for Arkham and CryptoQuant pure helper functions."""

from __future__ import annotations

import pytest

from src.intelligence.onchain.arkham_provider import (
    _herfindahl,
    _sum_usd,
    _sum_usd_direction,
    _zscore,
)
from src.intelligence.onchain.cryptoquant_provider import (
    _extract_binance_funding,
    _extract_rows,
    _miner_signal,
    _mvrv_stress_contrib,
    _netflow_zscore,
    _reserve_ratio,
)

# ---------------------------------------------------------------------------
# Arkham helpers
# ---------------------------------------------------------------------------


class TestSumUsd:
    def test_empty_data_returns_zero(self) -> None:
        assert _sum_usd({}) == 0.0

    def test_null_transfers_returns_zero(self) -> None:
        assert _sum_usd({"transfers": None}) == 0.0

    def test_sums_all_transfers(self) -> None:
        data = {"transfers": [{"usdValue": 100}, {"usdValue": 200.5}]}
        assert _sum_usd(data) == pytest.approx(300.5)

    def test_missing_usd_value_treated_as_zero(self) -> None:
        data = {"transfers": [{"other": "field"}, {"usdValue": 50}]}
        assert _sum_usd(data) == pytest.approx(50.0)


class TestSumUsdDirection:
    def test_filters_by_direction_in(self) -> None:
        transfers = [
            {"usdValue": 100, "direction": "in"},
            {"usdValue": 200, "direction": "out"},
        ]
        assert _sum_usd_direction({"transfers": transfers}, "in") == pytest.approx(100.0)

    def test_filters_by_direction_out(self) -> None:
        transfers = [
            {"usdValue": 100, "direction": "in"},
            {"usdValue": 200, "direction": "out"},
        ]
        assert _sum_usd_direction({"transfers": transfers}, "out") == pytest.approx(200.0)

    def test_no_matching_direction_returns_zero(self) -> None:
        data = {"transfers": [{"usdValue": 100, "direction": "in"}]}
        assert _sum_usd_direction(data, "out") == 0.0

    def test_empty_returns_zero(self) -> None:
        assert _sum_usd_direction({}, "in") == 0.0


class TestZscore:
    def test_empty_history_returns_zero(self) -> None:
        assert _zscore(5.0, []) == 0.0

    def test_single_element_returns_zero(self) -> None:
        assert _zscore(5.0, [5.0]) == 0.0

    def test_above_mean_is_positive(self) -> None:
        history = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _zscore(10.0, history) > 0.0

    def test_below_mean_is_negative(self) -> None:
        history = [10.0, 20.0, 30.0]
        assert _zscore(1.0, history) < 0.0

    def test_constant_history_no_div_zero(self) -> None:
        history = [5.0, 5.0, 5.0]
        # std=0, uses _EPS, result is a large finite number
        result = _zscore(5.0, history)
        assert isinstance(result, float)


class TestHerfindahl:
    def test_empty_data_returns_zero(self) -> None:
        assert _herfindahl({}) == 0.0

    def test_single_bucket_returns_zero(self) -> None:
        data = {"histogram": [{"usdValue": 1000}]}
        result = _herfindahl(data)
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_equal_buckets_near_zero(self) -> None:
        buckets = [{"usdValue": 100.0}] * 10
        result = _herfindahl({"histogram": buckets})
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_monopoly_near_one(self) -> None:
        buckets = [{"usdValue": 1_000_000}] + [{"usdValue": 1}] * 9
        result = _herfindahl({"histogram": buckets})
        assert result > 0.8

    def test_result_in_range(self) -> None:
        buckets = [{"usdValue": float(i * 100)} for i in range(1, 6)]
        result = _herfindahl({"histogram": buckets})
        assert 0.0 <= result <= 1.0

    def test_buckets_key_also_works(self) -> None:
        data = {"buckets": [{"usdValue": 500}, {"usdValue": 500}]}
        assert _herfindahl(data) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# CryptoQuant helpers
# ---------------------------------------------------------------------------


class TestCqExtractRows:
    def test_no_result_key_returns_empty(self) -> None:
        assert _extract_rows({}) == []

    def test_result_with_data_key(self) -> None:
        data = {"result": {"data": [{"a": 1}]}}
        assert _extract_rows(data) == [{"a": 1}]

    def test_result_is_list(self) -> None:
        data = {"result": [{"b": 2}]}
        assert _extract_rows(data) == [{"b": 2}]

    def test_result_is_none_returns_empty(self) -> None:
        assert _extract_rows({"result": None}) == []


class TestReserveRatio:
    def test_empty_returns_default(self) -> None:
        assert _reserve_ratio({}) == 0.5

    def test_large_reserve_clamps_to_one(self) -> None:
        rows = [{"reserve_usd": 1e20, "price": 60000}]
        assert _reserve_ratio({"result": rows}) == 1.0

    def test_zero_reserve_returns_zero(self) -> None:
        rows = [{"reserve_usd": 0, "price": 60000}]
        assert _reserve_ratio({"result": rows}) == 0.0

    def test_reasonable_value(self) -> None:
        # 10B reserve at 60k price → 10B/(21M*60k)≈0.0079
        rows = [{"reserve_usd": 10_000_000_000, "price": 60_000}]
        result = _reserve_ratio({"result": rows})
        assert 0.0 < result < 1.0


class TestNetflowZscore:
    def test_empty_returns_zero(self) -> None:
        assert _netflow_zscore({}) == 0.0

    def test_single_row_returns_zero(self) -> None:
        assert _netflow_zscore({"result": [{"netflow_usd": 100}]}) == 0.0

    def test_high_recent_netflow_positive(self) -> None:
        rows = [{"netflow_usd": 10.0}] * 20 + [{"netflow_usd": 1000.0}] * 7
        result = _netflow_zscore({"result": rows})
        assert result > 0.0


class TestMinerSignal:
    def test_empty_returns_zero(self) -> None:
        assert _miner_signal({}) == 0.0

    def test_constant_returns_zero(self) -> None:
        rows = [{"netflow_usd": 100.0}] * 5
        assert _miner_signal({"result": rows}) == pytest.approx(0.0, abs=1e-6)

    def test_result_clamped_to_minus_one_one(self) -> None:
        rows = [{"netflow_usd": 1.0}] * 10 + [{"netflow_usd": 1_000_000.0}]
        result = _miner_signal({"result": rows})
        assert -1.0 <= result <= 1.0


class TestExtractBinanceFunding:
    def test_empty_returns_none(self) -> None:
        assert _extract_binance_funding({}) is None

    def test_extracts_binance_row(self) -> None:
        rows = [
            {"exchange": "OKX", "funding_rate": 0.001},
            {"exchange": "Binance", "funding_rate": 0.0025},
        ]
        result = _extract_binance_funding({"result": rows})
        assert result == pytest.approx(0.0025)

    def test_case_insensitive_match(self) -> None:
        rows = [{"exchange": "binance_futures", "fundingRate": 0.003}]
        result = _extract_binance_funding({"result": rows})
        assert result == pytest.approx(0.003)

    def test_no_binance_row_returns_none(self) -> None:
        rows = [{"exchange": "kraken", "funding_rate": 0.002}]
        assert _extract_binance_funding({"result": rows}) is None


class TestMvrvStressContrib:
    def test_empty_returns_zero(self) -> None:
        assert _mvrv_stress_contrib({}) == 0.0

    def test_below_threshold_returns_zero(self) -> None:
        rows = [{"market_cap": 700, "realized_cap": 400}]  # mvrv≈1.75 < 3.5
        assert _mvrv_stress_contrib({"result": rows}) == 0.0

    def test_above_threshold_returns_positive(self) -> None:
        rows = [{"market_cap": 7000, "realized_cap": 1000}]  # mvrv=7 > 3.5
        result = _mvrv_stress_contrib({"result": rows})
        assert result > 0.0

    def test_capped_at_max(self) -> None:
        rows = [{"market_cap": 1e12, "realized_cap": 1}]  # extreme mvrv
        result = _mvrv_stress_contrib({"result": rows})
        assert result == pytest.approx(0.3)
