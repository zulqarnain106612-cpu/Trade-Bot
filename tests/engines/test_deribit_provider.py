"""Tests for DeribitProvider's fetch path (E-12's options chain).

The network path was uncovered, which is the same blind spot that hid E-18's
missing producer and E-10's units error — a provider that silently returns
nothing looks identical to one that is merely quiet.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.deribit_provider import DeribitProvider


def _session(responses):
    """Session whose successive .get() calls yield the given JSON bodies."""
    resps = []
    for body in responses:
        r = AsyncMock()
        r.json = AsyncMock(return_value=body)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        resps.append(r)
    s = AsyncMock()
    s.get = MagicMock(side_effect=resps)
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    return s


_INSTRUMENT = {
    "instrument_name": "BTC-27JUN25-100000-C",
    "expiration_timestamp": 1751011200000,
    "strike": 100000,
    "option_type": "call",
}
_BOOK = {
    "mark_iv": 55.0,
    "open_interest": 120.0,
    "stats": {"volume": 42.0},
    "greeks": {"delta": 0.45, "gamma": 0.0001},
}


class TestSupports:
    def test_btc_and_eth_supported(self):
        p = DeribitProvider()
        assert p.supports("BTC/USDT")
        assert p.supports("ETH/USDT")

    def test_other_coins_unsupported(self):
        """LTC/XMR have no Deribit options — E-12 redistributes weight (G-05)."""
        p = DeribitProvider()
        assert not p.supports("LTC/USDT")
        assert not p.supports("XMR/USDT")

    @pytest.mark.asyncio
    async def test_fetch_unsupported_returns_none_without_network(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession") as sess:
            assert await p.fetch("LTC/USDT") is None
        sess.assert_not_called()


class TestFetchChain:
    @pytest.mark.asyncio
    async def test_fetch_builds_chain_and_persists(self, tmp_path):
        from src.data.provider_cache import get_provider_cache

        p = DeribitProvider(data_root=tmp_path)
        with patch(
            "aiohttp.ClientSession",
            return_value=_session([{"result": [_INSTRUMENT]}, {"result": _BOOK}]),
        ):
            df = await p.fetch("BTC/USDT")

        assert df is not None and len(df) == 1
        assert df.iloc[0]["instrument"] == _INSTRUMENT["instrument_name"]
        assert df.iloc[0]["iv"] == pytest.approx(55.0)
        assert df.iloc[0]["delta"] == pytest.approx(0.45)
        assert (tmp_path / "options" / "BTC").exists()
        assert get_provider_cache().get_options("BTC") is not None

    @pytest.mark.asyncio
    async def test_zero_iv_row_is_dropped_by_quality_gate(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        book = {**_BOOK, "mark_iv": 0.0}
        with patch(
            "aiohttp.ClientSession",
            return_value=_session([{"result": [_INSTRUMENT]}, {"result": book}]),
        ):
            assert await p.fetch("BTC/USDT") is None

    @pytest.mark.asyncio
    async def test_zero_open_interest_row_is_dropped(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        book = {**_BOOK, "open_interest": 0.0}
        with patch(
            "aiohttp.ClientSession",
            return_value=_session([{"result": [_INSTRUMENT]}, {"result": book}]),
        ):
            assert await p.fetch("BTC/USDT") is None

    @pytest.mark.asyncio
    async def test_missing_orderbook_is_skipped(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        with patch(
            "aiohttp.ClientSession",
            return_value=_session([{"result": [_INSTRUMENT]}, {}]),
        ):
            assert await p.fetch("BTC/USDT") is None

    @pytest.mark.asyncio
    async def test_no_instruments_yields_none(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_session([{"result": []}])):
            assert await p.fetch("BTC/USDT") is None

    @pytest.mark.asyncio
    async def test_instruments_error_is_swallowed(self, tmp_path):
        p = DeribitProvider(data_root=tmp_path)
        s = AsyncMock()
        s.get = MagicMock(side_effect=RuntimeError("upstream down"))
        s.__aenter__ = AsyncMock(return_value=s)
        s.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=s):
            assert await p.fetch("BTC/USDT") is None

    @pytest.mark.asyncio
    async def test_stale_cache_served_when_refresh_fails(self, tmp_path):
        """A failed refresh must not blank out a chain we already hold."""
        p = DeribitProvider(data_root=tmp_path)
        with patch(
            "aiohttp.ClientSession",
            return_value=_session([{"result": [_INSTRUMENT]}, {"result": _BOOK}]),
        ):
            await p.fetch("BTC/USDT")

        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            again = await p.fetch("BTC/USDT")
        assert again is not None and len(again) == 1


@pytest.mark.asyncio
async def test_run_loop_polls_then_cancels(tmp_path):
    import asyncio

    p = DeribitProvider(data_root=tmp_path)
    with (
        patch.object(DeribitProvider, "fetch", new=AsyncMock(return_value=None)) as f,
        patch("src.data.deribit_provider._POLL_INTERVAL", 0.01),
    ):
        task = asyncio.create_task(p.run_loop("BTC/USDT"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert f.await_count >= 1
