"""
OCI-006 — CoinglassProvider unit tests.
All HTTP calls are mocked; no network required.
"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, patch
from src.intelligence.onchain.coinglass_provider import (
    CoinglassProvider,
    _extract_list,
    _oi_change_pct,
    _liq_zscore,
    _heatmap_max,
    _extract_funding,
    _ls_ratio,
)


# ---------------------------------------------------------------------------
# _extract_list
# ---------------------------------------------------------------------------

class TestExtractList:
    def test_data_list(self):
        assert _extract_list({"data": [{"a": 1}]}) == [{"a": 1}]

    def test_no_data_key(self):
        assert _extract_list({"x": 1}) == []

    def test_nested_dict(self):
        result = _extract_list({"data": {"rows": [1, 2, 3]}})
        assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# _oi_change_pct
# ---------------------------------------------------------------------------

class TestOiChangePct:
    def test_insufficient(self):
        assert _oi_change_pct({"data": [{"c": 100.0}]}) == 0.0

    def test_increase(self):
        rows = [{"c": float(i * 10 + 100)} for i in range(48)]
        r = _oi_change_pct({"data": rows})
        assert r > 0

    def test_decrease(self):
        rows = [{"c": float(1000 - i * 10)} for i in range(48)]
        r = _oi_change_pct({"data": rows})
        assert r < 0

    def test_list_row_format(self):
        rows = [[0, 0, 0, float(i * 10 + 100)] for i in range(48)]
        r = _oi_change_pct({"data": rows})
        assert r > 0


# ---------------------------------------------------------------------------
# _liq_zscore
# ---------------------------------------------------------------------------

class TestLiqZscore:
    def test_insufficient(self):
        assert _liq_zscore({"data": [{"buyLiquidationUsd": 1}]}) == 0.0

    def test_spike(self):
        rows = [{"buyLiquidationUsd": 0.0, "sellLiquidationUsd": 0.0}] * 23
        rows.append({"buyLiquidationUsd": 1e9, "sellLiquidationUsd": 1e9})
        r = _liq_zscore({"data": rows})
        assert r > 0

    def test_uniform(self):
        rows = [{"buyLiquidationUsd": 100.0, "sellLiquidationUsd": 100.0}] * 24
        r = _liq_zscore({"data": rows})
        assert r == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# _heatmap_max
# ---------------------------------------------------------------------------

class TestHeatmapMax:
    def test_empty(self):
        assert _heatmap_max({"data": []}) == 0.0

    def test_list_format(self):
        rows = [[30_000, 100, 5e6], [31_000, 200, 8e6]]
        assert _heatmap_max({"data": rows}) == pytest.approx(8e6)

    def test_dict_format(self):
        rows = [{"liquidationUsd": 3e6}, {"liquidationUsd": 9e6}]
        assert _heatmap_max({"data": rows}) == pytest.approx(9e6)


# ---------------------------------------------------------------------------
# _extract_funding
# ---------------------------------------------------------------------------

class TestExtractFunding:
    def test_none_on_empty(self):
        assert _extract_funding({"data": []}) is None

    def test_direct_field(self):
        assert _extract_funding({"data": [{"fundingRate": 0.015}]}) == pytest.approx(0.015)

    def test_exchange_list_avg(self):
        row = {"exchangeList": [{"fundingRate": 0.01}, {"fundingRate": 0.03}]}
        assert _extract_funding({"data": [row]}) == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# _ls_ratio
# ---------------------------------------------------------------------------

class TestLsRatio:
    def test_empty(self):
        assert _ls_ratio({"data": []}) == pytest.approx(1.0)

    def test_ratio(self):
        r = _ls_ratio({"data": [{"longRatio": 60.0, "shortRatio": 40.0}]})
        assert r == pytest.approx(1.5)

    def test_zero_short(self):
        r = _ls_ratio({"data": [{"longRatio": 60.0, "shortRatio": 0.0}]})
        assert r == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# CoinglassProvider — disabled
# ---------------------------------------------------------------------------

class TestCoinglassProviderDisabled:
    def setup_method(self):
        self.prov = CoinglassProvider(api_key="")

    @pytest.mark.asyncio
    async def test_fetch_metrics_neutral(self):
        m = await self.prov.fetch_metrics()
        assert m["confidence"] == 0.0
        assert m["futures_oi_change_pct"] == 0.0
        assert m["whale_buy_sell_ratio"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_initialize_noop(self):
        await self.prov.initialize()

    def test_exchange_id(self):
        assert self.prov.exchange_id == "coinglass"


# ---------------------------------------------------------------------------
# CoinglassProvider — enabled (mocked)
# ---------------------------------------------------------------------------

class TestCoinglassProviderEnabled:
    def _make_provider(self):
        return CoinglassProvider(api_key="cg-test-key")

    @pytest.mark.asyncio
    async def test_fetch_metrics_full(self):
        prov = self._make_provider()
        oi_rows = [{"c": float(i + 100)} for i in range(48)]
        liq_rows = [{"buyLiquidationUsd": 1e6, "sellLiquidationUsd": 5e5}] * 24
        heatmap_rows = [[30000, 100, 5e7]]
        fr_rows = [{"fundingRate": 0.01}]
        ls_rows = [{"longRatio": 55.0, "shortRatio": 45.0}]

        responses = [
            {"data": oi_rows},
            {"data": liq_rows},
            {"data": heatmap_rows},
            {"data": fr_rows},
            {"data": ls_rows},
        ]
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            r = responses[call_count % len(responses)]
            call_count += 1
            return r

        with patch.object(prov, '_get', side_effect=mock_get):
            m = await prov.fetch_metrics()

        assert m["confidence"] == pytest.approx(1.0)
        assert isinstance(m["futures_oi_change_pct"], float)
        assert m["liquidation_cascade_risk_usd"] == pytest.approx(5e7)
        assert m["binance_funding_rate_pct"] == pytest.approx(0.01)
        assert m["whale_buy_sell_ratio"] > 1.0

    @pytest.mark.asyncio
    async def test_partial_failure_degrades_confidence(self):
        prov = self._make_provider()
        call_count = 0
        responses = [
            None,  # OI fails
            {"data": []},
            {"data": []},
            {"data": []},
            {"data": []},
        ]

        async def mock_get(url, **kwargs):
            nonlocal call_count
            r = responses[call_count % len(responses)]
            call_count += 1
            return r

        with patch.object(prov, '_get', side_effect=mock_get):
            m = await prov.fetch_metrics()

        assert m["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_initialize_warms_cache(self):
        prov = self._make_provider()
        mock_get = AsyncMock(return_value={"data": []})
        with patch.object(prov, '_get', mock_get):
            await prov.initialize()
        mock_get.assert_called_once()
