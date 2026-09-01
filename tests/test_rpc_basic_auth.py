"""The bitcoind RPC clients must not use the deprecated ``aiohttp.BasicAuth``.

``BasicAuth`` is deprecated in aiohttp 3.14 and removed in 4.0, and this
project promotes ``DeprecationWarning`` raised from ``src.*`` to an error, so
constructing one aborted the request and made every live call fall into the
"node is down" branch.  These tests keep the real ``aiohttp`` module in place
and only fake the transport, so the deprecation would fire for real.
"""

import base64

import pytest

from src.features._rpc_auth import basic_auth_header

_EXPECTED = "Basic " + base64.b64encode(b"rpcuser:rpcpass").decode()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Session:
    """Records the headers each request was sent with."""

    seen: list[dict] = []

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, _url, json, headers=None, **_kwargs):
        type(self).seen.append(headers)
        method = json["method"]
        if method == "getmempoolinfo":
            return _Resp({"result": {"size": 7, "bytes": 2048}})
        if method == "estimatesmartfee":
            return _Resp({"result": {"feerate": 0.0002}})
        return _Resp({"result": {"ok": True}})


class _AwaitableSession(_Session):
    """``mempool`` awaits ``post`` directly rather than using it as a context."""

    async def post(self, url, json, headers=None, **kwargs):  # type: ignore[override]
        return _Session.post(self, url, json, headers=headers, **kwargs)


def test_the_header_helper_encodes_credentials():
    assert basic_auth_header("rpcuser", "rpcpass") == {"Authorization": _EXPECTED}


def test_the_helper_falls_back_when_aiohttp_has_no_encoder(monkeypatch):
    import aiohttp

    monkeypatch.delattr(aiohttp, "encode_basic_auth", raising=False)
    assert basic_auth_header("rpcuser", "rpcpass") == {"Authorization": _EXPECTED}


@pytest.mark.asyncio
async def test_mempool_features_are_real_and_authenticated(monkeypatch):
    import aiohttp

    from src.features import mempool

    _AwaitableSession.seen = []
    monkeypatch.setattr(aiohttp, "ClientSession", _AwaitableSession)

    feats = await mempool.fetch_mempool_features(
        rpc_url="http://node/", rpc_user="rpcuser", rpc_pass="rpcpass"
    )

    # a live node must not be reported as an empty mempool
    assert feats.tx_count == 7
    assert feats.mempool_bytes == 2048
    assert _AwaitableSession.seen == [{"Authorization": _EXPECTED}] * 2


@pytest.mark.asyncio
async def test_the_rpc_client_authenticates_with_a_header(monkeypatch):
    import aiohttp

    from src.features.onchain import BitcoinRPCClient

    _Session.seen = []
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)

    client = BitcoinRPCClient(url="http://node/", user="rpcuser", password="rpcpass")
    assert await client.get_blockchain_info() == {"ok": True}
    assert _Session.seen == [{"Authorization": _EXPECTED}]
