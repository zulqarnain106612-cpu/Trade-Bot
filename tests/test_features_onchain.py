"""Tests for src/features/onchain.py -- SOPR / NVT / MVRV from bitcoind RPC.

No test reaches a real node: BitcoinRPCClient is either a fake object or
has aiohttp.ClientSession patched out. aiohttp.BasicAuth is patched in the
_call tests for the same reason as tests/test_features_mempool.py -- the
deprecated constructor raises under this repo's error-on-DeprecationWarning
policy for src.*.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.features.onchain import BitcoinRPCClient, OnChainFeatureExtractor


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_request: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json, auth, **_kwargs):
        self.last_request = json
        return _FakeResponse(self._payload)


async def test_call_returns_result_field():
    session = _FakeSession({"result": {"blocks": 900_000}})
    with patch("aiohttp.ClientSession", return_value=session), patch("aiohttp.BasicAuth"):
        client = BitcoinRPCClient()
        result = await client._call("getblockchaininfo")
    assert result == {"blocks": 900_000}
    assert session.last_request["method"] == "getblockchaininfo"


async def test_call_raises_on_rpc_error():
    session = _FakeSession({"error": {"code": -1, "message": "bad"}})
    with patch("aiohttp.ClientSession", return_value=session), patch("aiohttp.BasicAuth"):
        client = BitcoinRPCClient()
        with pytest.raises(RuntimeError, match="RPC error"):
            await client._call("getblockchaininfo")


async def test_call_passes_params_through():
    session = _FakeSession({"result": "hash"})
    with patch("aiohttp.ClientSession", return_value=session), patch("aiohttp.BasicAuth"):
        client = BitcoinRPCClient()
        await client._call("getblockhash", [900_000])
    assert session.last_request["params"] == [900_000]


async def test_rpc_convenience_methods_delegate_to_call():
    client = BitcoinRPCClient()
    with patch.object(client, "_call", new=AsyncMock(return_value="ok")) as mock_call:
        assert await client.get_blockchain_info() == "ok"
        assert await client.list_unspent() == "ok"
        assert await client.get_block_stats("hash") == "ok"
        assert await client.get_block_hash(1) == "ok"
        assert await client.get_best_block_hash() == "ok"
    methods = [c.args[0] for c in mock_call.await_args_list]
    assert methods == [
        "getblockchaininfo",
        "listunspent",
        "getblockstats",
        "getblockhash",
        "getbestblockhash",
    ]


def _fake_rpc(info=None, stats=None, utxos=None) -> MagicMock:
    rpc = MagicMock()
    rpc.get_blockchain_info = AsyncMock(return_value=info or {"bestblockhash": "abc"})
    rpc.get_block_stats = AsyncMock(return_value=stats or {"total_out": 5_000_000_000})
    rpc.list_unspent = AsyncMock(return_value=utxos if utxos is not None else [])
    return rpc


async def test_compute_happy_path():
    rpc = _fake_rpc(utxos=[{"amount": 1.0}, {"amount": 2.0}])
    extractor = OnChainFeatureExtractor(rpc=rpc)
    feats = await extractor.compute(spot_price_usd=50_000.0, market_cap_usd=1e12)

    assert feats.sopr == 1.0
    assert feats.nvt > 0
    # realised cap = 3 BTC * 50k = 150k -> mvrv = 1e12 / 150k
    assert feats.mvrv == pytest.approx(1e12 / 150_000.0)


async def test_compute_falls_back_on_rpc_failure():
    rpc = _fake_rpc()
    rpc.get_blockchain_info = AsyncMock(side_effect=RuntimeError("node down"))
    extractor = OnChainFeatureExtractor(rpc=rpc)
    feats = await extractor.compute(50_000.0, 1e12)
    assert (feats.sopr, feats.nvt, feats.mvrv) == (1.0, 50.0, 1.0)


async def test_compute_caches_realised_cap():
    rpc = _fake_rpc(utxos=[{"amount": 4.0}])
    extractor = OnChainFeatureExtractor(rpc=rpc)
    await extractor.compute(10_000.0, 1e9)
    assert extractor._realised_cap_cache == 40_000.0


async def test_compute_with_zero_tx_volume_does_not_divide_by_zero():
    rpc = _fake_rpc(stats={"total_out": 0})
    extractor = OnChainFeatureExtractor(rpc=rpc)
    feats = await extractor.compute(50_000.0, 1e12)
    assert feats.nvt == 1e12  # divided by the 1.0 floor


def test_estimate_realised_cap_empty_utxos_uses_midpoint_heuristic():
    extractor = OnChainFeatureExtractor(rpc=_fake_rpc())
    assert extractor._estimate_realised_cap([], 100.0) == 100.0 * 21_000_000 * 0.5


def test_estimate_realised_cap_sums_utxo_amounts():
    extractor = OnChainFeatureExtractor(rpc=_fake_rpc())
    utxos = [{"amount": 1.5}, {"amount": 2.5}, {"no_amount": 1}]
    assert extractor._estimate_realised_cap(utxos, 1000.0) == 4000.0


def test_approximate_sopr_returns_baseline():
    extractor = OnChainFeatureExtractor(rpc=_fake_rpc())
    assert extractor._approximate_sopr(50_000.0) == 1.0


def test_extractor_builds_default_rpc_client_when_none_given():
    extractor = OnChainFeatureExtractor()
    assert isinstance(extractor._rpc, BitcoinRPCClient)
