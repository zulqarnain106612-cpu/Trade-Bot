"""
OCI-005 — CryptoQuantProvider unit tests.
All HTTP calls are mocked; no network required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.intelligence.onchain.cryptoquant_provider import (
    CryptoQuantProvider,
    _extract_binance_funding,
    _extract_rows,
    _miner_signal,
    _mvrv_stress_contrib,
    _netflow_zscore,
    _reserve_ratio,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rows(n: int, value: float = 1_000.0, key: str = "netflow_usd") -> dict:
    return {"result": {"data": [{key: value} for _ in range(n)]}}


def _mvrv_row(mvrv: float) -> dict:
    # _mvrv_stress_contrib computes mvrv = market_cap / realized_cap
    # for mvrv=4.0: set market_cap=4, realized_cap=1
    realized = 1.0
    market = mvrv * realized
    return {"result": {"data": [{"market_cap": market, "realized_cap": realized}]}}


# ---------------------------------------------------------------------------
# _extract_rows
# ---------------------------------------------------------------------------


class TestExtractRows:
    def test_nested_data(self):
        assert _extract_rows({"result": {"data": [{"a": 1}]}}) == [{"a": 1}]

    def test_result_list(self):
        assert _extract_rows({"result": [{"a": 1}]}) == [{"a": 1}]

    def test_missing_returns_empty(self):
        assert _extract_rows({}) == []

    def test_wrong_type_returns_empty(self):
        assert _extract_rows({"result": 42}) == []


# ---------------------------------------------------------------------------
# _reserve_ratio
# ---------------------------------------------------------------------------


class TestReserveRatio:
    def test_empty(self):
        assert _reserve_ratio({"result": {"data": []}}) == 0.5

    def test_clamp_high(self):
        # 21M BTC * 60k price = 1.26e12 market cap; reserve > that → clamped to 1.0
        data = {"result": {"data": [{"reserve_usd": 2e15, "price": 60_000}]}}
        assert _reserve_ratio(data) == 1.0

    def test_clamp_low(self):
        data = {"result": {"data": [{"reserve_usd": 0, "price": 60_000}]}}
        assert _reserve_ratio(data) == 0.0

    def test_typical(self):
        r = _reserve_ratio({"result": {"data": [{"reserve_usd": 1e10, "price": 60_000}]}})
        assert 0.0 < r < 1.0


# ---------------------------------------------------------------------------
# _netflow_zscore
# ---------------------------------------------------------------------------


class TestNetflowZscore:
    def test_insufficient_rows(self):
        assert _netflow_zscore({"result": {"data": [{"netflow_usd": 1}]}}) == 0.0

    def test_uniform_zero_std(self):
        # All same value → std ≈ 0 → falls back to _EPS denominator, result is 0
        data = {"result": {"data": [{"netflow_usd": 100.0} for _ in range(30)]}}
        assert _netflow_zscore(data) == pytest.approx(0.0, abs=1e-6)

    def test_spike_positive(self):
        # 23 rows of 0, then 7 large positive rows → positive z-score
        rows = [{"netflow_usd": 0.0}] * 23 + [{"netflow_usd": 1e6}] * 7
        data = {"result": {"data": rows}}
        assert _netflow_zscore(data) > 0


# ---------------------------------------------------------------------------
# _miner_signal
# ---------------------------------------------------------------------------


class TestMinerSignal:
    def test_insufficient_rows(self):
        assert _miner_signal({"result": {"data": [{"netflow_usd": 1}]}}) == 0.0

    def test_clamped_range(self):
        rows = [{"netflow_usd": 0.0}] * 29 + [{"netflow_usd": 1e9}]
        data = {"result": {"data": rows}}
        s = _miner_signal(data)
        assert -1.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# _extract_binance_funding
# ---------------------------------------------------------------------------


class TestExtractBinanceFunding:
    def test_found(self):
        data = {
            "result": {
                "data": [
                    {"exchange": "OKX", "funding_rate": 0.1},
                    {"exchange": "Binance", "funding_rate": 0.05},
                ]
            }
        }
        assert _extract_binance_funding(data) == pytest.approx(0.05)

    def test_not_found(self):
        data = {"result": {"data": [{"exchange": "OKX", "funding_rate": 0.1}]}}
        assert _extract_binance_funding(data) is None

    def test_alternate_key(self):
        data = {"result": {"data": [{"exchange": "Binance", "fundingRate": 0.03}]}}
        assert _extract_binance_funding(data) == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# _mvrv_stress_contrib
# ---------------------------------------------------------------------------


class TestMvrvStressContrib:
    def test_empty(self):
        assert _mvrv_stress_contrib({"result": {"data": []}}) == 0.0

    def test_low_mvrv(self):
        assert _mvrv_stress_contrib(_mvrv_row(1.5)) == 0.0

    def test_high_mvrv(self):
        # threshold=3.5, cap=0.3; need (mvrv-3.5)/10 >= 0.3 → mvrv >= 6.5
        assert _mvrv_stress_contrib(_mvrv_row(6.5)) == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# CryptoQuantProvider — disabled (no key)
# ---------------------------------------------------------------------------


class TestCryptoQuantProviderDisabled:
    def setup_method(self):
        self.prov = CryptoQuantProvider(api_key="")

    @pytest.mark.asyncio
    async def test_fetch_metrics_returns_neutral(self):
        m = await self.prov.fetch_metrics()
        assert m["confidence"] == 0.0
        assert m["exchange_reserve_ratio"] == 0.5
        assert m["exchange_netflow_7d_zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_initialize_noop(self):
        await self.prov.initialize()  # must not raise

    @pytest.mark.asyncio
    async def test_close_noop(self):
        await self.prov.close()

    def test_exchange_id(self):
        assert self.prov.exchange_id == "cryptoquant"


# ---------------------------------------------------------------------------
# CryptoQuantProvider — enabled (mocked HTTP)
# ---------------------------------------------------------------------------


class TestCryptoQuantProviderEnabled:
    def _make_provider(self):
        return CryptoQuantProvider(api_key="test-key-123")  # pragma: allowlist secret

    def _reserve_data(self):
        return {"result": {"data": [{"reserve_usd": 5e9, "price": 60_000}]}}

    def _netflow_data(self):
        return {"result": {"data": [{"netflow_usd": float(i)} for i in range(30)]}}

    def _miner_data(self):
        return {"result": {"data": [{"netflow_usd": float(i * 100)} for i in range(30)]}}

    def _funding_data(self):
        return {"result": {"data": [{"exchange": "Binance", "funding_rate": 0.02}]}}

    def _mvrv_data(self):
        return {"result": {"data": [{"mvrv": 2.0}]}}

    @pytest.mark.asyncio
    async def test_fetch_metrics_full(self):
        prov = self._make_provider()
        responses = [
            self._reserve_data(),
            self._netflow_data(),
            self._miner_data(),
            self._funding_data(),
            self._mvrv_data(),
        ]
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            r = responses[call_count % len(responses)]
            call_count += 1
            return r

        with patch.object(prov, "_get", side_effect=mock_get):
            m = await prov.fetch_metrics()

        assert m["confidence"] == pytest.approx(1.0)
        assert 0.0 <= m["exchange_reserve_ratio"] <= 1.0
        assert isinstance(m["exchange_netflow_7d_zscore"], float)
        assert -1.0 <= m["miner_netflow_signal"] <= 1.0
        assert m["binance_funding_rate_pct"] == pytest.approx(0.02)
        assert "timestamp" in m

    @pytest.mark.asyncio
    async def test_fetch_metrics_partial_failure(self):
        """One endpoint returning None degrades confidence but does not raise."""
        prov = self._make_provider()
        call_count = 0
        responses = [
            None,  # reserve fails
            {"result": {"data": [{"netflow_usd": 1.0} for _ in range(30)]}},
            {"result": {"data": [{"netflow_usd": 1.0} for _ in range(30)]}},
            {"result": {"data": [{"exchange": "Binance", "funding_rate": 0.01}]}},
            {"result": {"data": [{"mvrv": 1.0}]}},
        ]

        async def mock_get(url, **kwargs):
            nonlocal call_count
            r = responses[call_count % len(responses)]
            call_count += 1
            return r

        with patch.object(prov, "_get", side_effect=mock_get):
            m = await prov.fetch_metrics()

        assert m["confidence"] < 1.0
        assert m["exchange_reserve_ratio"] == pytest.approx(0.5)  # neutral fallback

    @pytest.mark.asyncio
    async def test_initialize_warms_cache(self):
        prov = self._make_provider()
        mock_get = AsyncMock(return_value={"result": {"data": []}})
        with patch.object(prov, "_get", mock_get):
            await prov.initialize()
        mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_metrics_all_endpoints_fail(self):
        """All _get calls return None → all 5 confidence penalties applied (5 * 0.05 = 0.25 off)."""
        prov = self._make_provider()

        async def mock_get(url, **kwargs):
            return None

        with patch.object(prov, "_get", side_effect=mock_get):
            m = await prov.fetch_metrics()

        # Each of 5 endpoints penalises 0.05 → 1.0 - 0.25 = 0.75
        assert m["confidence"] == pytest.approx(0.75)
        # Neutral fallbacks applied
        assert m["exchange_reserve_ratio"] == pytest.approx(0.5)
        assert m["exchange_netflow_7d_zscore"] == pytest.approx(0.0)
        assert m["miner_netflow_signal"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_fetch_metrics_funding_no_binance_row(self):
        """Funding data returned but no Binance exchange row → key stays at neutral 0.0."""
        prov = self._make_provider()
        responses = [
            {"result": {"data": [{"reserve_usd": 5e9, "price": 60_000}]}},
            {"result": {"data": [{"netflow_usd": 1.0}] * 30}},
            {"result": {"data": [{"netflow_usd": 1.0}] * 30}},
            # funding data with non-Binance exchange only
            {"result": {"data": [{"exchange": "OKX", "funding_rate": 0.01}]}},
            {"result": {"data": [{"market_cap": 1e12, "realized_cap": 5e11}]}},
        ]
        idx = 0

        async def mock_get(url, **kwargs):
            nonlocal idx
            r = responses[idx % len(responses)]
            idx += 1
            return r

        with patch.object(prov, "_get", side_effect=mock_get):
            m = await prov.fetch_metrics()

        # binance_funding_rate_pct stays at neutral 0.0 (fr was None, key was never updated)
        assert m["binance_funding_rate_pct"] == pytest.approx(0.0)
