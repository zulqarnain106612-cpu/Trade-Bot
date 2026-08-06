"""Tests for the two providers that fill previously-dead engine data keys.

E-18 read `exchange_flows` and E-10/E-04 read `block_height`; neither key had
a producer, so both engines ran on fallbacks forever. These cover the fetch,
persist and cache-update paths of the providers that now write them.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.block_height_provider import BlockHeightProvider
from src.data.exchange_flow_provider import ExchangeFlowProvider, build_flow_records


def _mock_session(*, json_body=None, text_body=None):
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    if json_body is not None:
        resp.json = AsyncMock(return_value=json_body)
    if text_body is not None:
        resp.text = AsyncMock(return_value=text_body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


_PAYLOAD = {
    "cexs": [
        {"name": "Binance", "currentTvl": 1e11, "inflows_24h": 2.5e8},
        {"name": "OKX", "currentTvl": 2e10, "inflows_24h": -8e7},
    ]
}


# ---------------------------------------------------------------------------
# ExchangeFlowProvider
# ---------------------------------------------------------------------------


class TestExchangeFlowProvider:
    @pytest.mark.asyncio
    async def test_fetch_populates_cache_and_persists(self, tmp_path):
        from src.data.provider_cache import get_provider_cache

        p = ExchangeFlowProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_PAYLOAD)):
            flows = await p.fetch_once()

        assert {f["exchange"] for f in flows} == {"Binance", "OKX"}
        assert get_provider_cache().get_exchange_flows() == flows
        written = list((tmp_path / "exchange_flows").glob("*.parquet"))
        assert len(written) == 1

    @pytest.mark.asyncio
    async def test_second_fetch_appends_to_same_day_file(self, tmp_path):
        import pandas as pd

        p = ExchangeFlowProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_PAYLOAD)):
            await p.fetch_once()
            await p.fetch_once()

        path = next((tmp_path / "exchange_flows").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 4  # 2 records x 2 fetches

    @pytest.mark.asyncio
    async def test_network_error_leaves_state_untouched(self, tmp_path):
        p = ExchangeFlowProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            assert await p.fetch_once() == []
        assert not (tmp_path / "exchange_flows").exists()

    @pytest.mark.asyncio
    async def test_empty_payload_is_not_persisted(self, tmp_path):
        """An upstream returning nothing must not overwrite good flows."""
        p = ExchangeFlowProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_PAYLOAD)):
            await p.fetch_once()
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body={"cexs": []})):
            await p.fetch_once()
        assert len(p.latest_flows()) == 2

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_break_fetch(self, tmp_path):
        p = ExchangeFlowProvider(data_root=tmp_path)
        with (
            patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_PAYLOAD)),
            patch.object(ExchangeFlowProvider, "_persist", side_effect=OSError("disk full")),
        ):
            # _persist raising is swallowed by _fetch's own guard
            await p.fetch_once()
        assert p.latest_flows() == []

    def test_window_selection_changes_records(self):
        payload = {
            "cexs": [
                {"name": "Binance", "currentTvl": 1e11, "inflows_24h": 5e8, "inflows_1w": -5e8}
            ]
        }
        day = build_flow_records(payload, "inflows_24h")
        week = build_flow_records(payload, "inflows_1w")
        assert day[0]["to"] == "Binance"
        assert week[0]["from"] == "Binance"

    def test_missing_window_key_yields_nothing(self):
        assert build_flow_records(_PAYLOAD, "inflows_nonexistent") == []

    def test_non_numeric_flow_is_skipped(self):
        payload = {"cexs": [{"name": "X", "currentTvl": 1e9, "inflows_24h": "lots"}]}
        assert build_flow_records(payload) == []

    def test_bare_list_payload_accepted(self):
        recs = build_flow_records(_PAYLOAD["cexs"])
        assert len(recs) == 2


# ---------------------------------------------------------------------------
# BlockHeightProvider
# ---------------------------------------------------------------------------


class TestBlockHeightProvider:
    @pytest.mark.asyncio
    async def test_fetch_sets_height_and_cache(self):
        from src.data.provider_cache import get_provider_cache

        p = BlockHeightProvider()
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body="961340\n")):
            height = await p.fetch_once()

        assert height == 961340
        assert get_provider_cache().get_block_height() == 961340

    @pytest.mark.asyncio
    async def test_implausible_height_rejected(self):
        """A parsed error page must not silently shift the emission epoch."""
        p = BlockHeightProvider()
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body="42")):
            assert await p.fetch_once() == 0

    @pytest.mark.asyncio
    async def test_non_numeric_body_rejected(self):
        p = BlockHeightProvider()
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body="<html>")):
            assert await p.fetch_once() == 0

    @pytest.mark.asyncio
    async def test_network_error_keeps_previous_height(self):
        p = BlockHeightProvider()
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body="961340")):
            await p.fetch_once()
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            assert await p.fetch_once() == 961340

    @pytest.mark.asyncio
    async def test_run_loop_polls_then_cancels(self):
        """Cover run_loop's body without letting it spin forever."""
        import asyncio

        p = BlockHeightProvider()
        with (
            patch("aiohttp.ClientSession", return_value=_mock_session(text_body="961340")),
            patch("src.data.block_height_provider._POLL_INTERVAL", 0.01),
        ):
            task = asyncio.create_task(p.run_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert p.latest_height() == 961340


@pytest.mark.asyncio
async def test_exchange_flow_run_loop_polls_then_cancels():
    import asyncio

    p = ExchangeFlowProvider()
    with (
        patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_PAYLOAD)),
        patch("src.data.exchange_flow_provider._POLL_INTERVAL", 0.01),
        patch.object(ExchangeFlowProvider, "_persist", MagicMock()),
    ):
        task = asyncio.create_task(p.run_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(p.latest_flows()) == 2
