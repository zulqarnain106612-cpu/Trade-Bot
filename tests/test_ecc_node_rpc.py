"""The two ECC paths that actually talk to bitcoind, with the node faked.

Every suite run so far exercised the *failure* half of these: with no node on
127.0.0.1:8332 the clusterer logged `secp256k1_rpc_failed` and returned the
neutral result, and the block fetch logged `ecc_block_fetch_failed` and
returned []. The lines were covered; the behaviour they encode was not. These
tests supply a healthy node so the success path is the one under test.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest


def _requests_module(handler):
    """A fake `requests` whose post() is answered by `handler(method, params)`."""
    mod = types.ModuleType("requests")
    calls: list[tuple[str, list]] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _post(url, json=None, auth=None, timeout=None):
        calls.append((json["method"], json["params"]))
        return _Resp(handler(json["method"], json["params"]))

    mod.post = _post
    mod.calls = calls
    return mod


# ---------------------------------------------------------------------------
# Secp256k1ClusterWorker.run
# ---------------------------------------------------------------------------


def _utxos():
    """Two addresses co-spent in one txid, plus a lone small one."""
    return [
        {"address": "bc1whaleA", "amount": 80.0, "txid": "t1", "vout": 0},
        {"address": "bc1whaleB", "amount": 60.0, "txid": "t1", "vout": 1},
        {"address": "bc1minnow", "amount": 0.5, "txid": "t2", "vout": 0},
    ]


def _run_clusterer(handler):
    from src.ecc.secp256k1_cluster import Secp256k1ClusterWorker

    worker = Secp256k1ClusterWorker(rpc_url="http://node:8332", rpc_user="u", rpc_pass="p")
    with patch.dict(sys.modules, {"requests": _requests_module(handler)}):
        return worker.run()


def test_a_healthy_node_yields_real_cluster_features():
    result = _run_clusterer(lambda _m, _p: {"result": _utxos()})

    # the two co-spent addresses are one owner, over the 100 BTC whale bar
    assert result.whale_count == 1
    assert result.total_whale_btc == pytest.approx(140.0)
    assert result.clusters
    assert result.utxos == _utxos()


def test_the_flow_score_is_structurally_always_zero():
    """Documents a live gap, so a future fix has to update this test.

    `flow_score` weights each whale cluster's `net_flow_btc`, which is
    `inflow_btc - outflow_btc`. Neither field is ever assigned: both
    clustering paths build `ClusterInfo` with only cluster_id, total_btc and
    addresses, and `listunspent` carries no flow history to fill them from.
    So the accumulation/distribution signal is a constant 0.0 no matter what
    the node reports -- and `cluster_flow_score` is one of the five ECC
    features the RL state reads.
    """
    from src.ecc.secp256k1_cluster import ClusterInfo

    result = _run_clusterer(lambda _m, _p: {"result": _utxos()})
    assert result.flow_score == pytest.approx(0.0)
    assert all(c.inflow_btc == 0.0 and c.outflow_btc == 0.0 for c in result.clusters)

    # the scoring itself is fine -- it is the inputs that are never supplied
    scored = ClusterInfo(cluster_id=0, total_btc=200.0, addresses=["a"], inflow_btc=100.0)
    from src.ecc.secp256k1_cluster import AddressClusterer

    assert AddressClusterer().flow_score([scored]) == pytest.approx(0.5)


def test_the_clusterer_asks_the_node_for_the_utxo_set():
    from src.ecc.secp256k1_cluster import Secp256k1ClusterWorker

    worker = Secp256k1ClusterWorker(rpc_url="http://node:8332", rpc_user="u", rpc_pass="p")
    fake = _requests_module(lambda _m, _p: {"result": _utxos()})
    with patch.dict(sys.modules, {"requests": fake}):
        worker.run()

    assert fake.calls == [("listunspent", [])]


def test_an_empty_utxo_set_is_not_an_error():
    result = _run_clusterer(lambda _m, _p: {"result": []})

    assert result.whale_count == 0
    assert result.total_whale_btc == pytest.approx(0.0)
    assert result.flow_score == pytest.approx(0.0)


def test_an_unreachable_node_returns_the_neutral_result():
    def _explode(_m, _p):
        raise ConnectionError("connection refused")

    result = _run_clusterer(_explode)

    assert result.whale_count == 0
    assert result.flow_score == pytest.approx(0.0)


def test_a_response_without_a_result_field_is_treated_as_a_failure():
    """bitcoind returns {"error": ...} with no "result" on a bad call."""
    result = _run_clusterer(lambda _m, _p: {"error": {"code": -32601}})

    assert result.whale_count == 0


# ---------------------------------------------------------------------------
# _fetch_recent_block_transactions
# ---------------------------------------------------------------------------


_TX = {"txid": "abc", "hex": "00", "vin": []}


def _block_handler(method, _params):
    if method == "getbestblockhash":
        return {"result": "0000beef"}
    if method == "getblock":
        return {"result": {"tx": [_TX]}}
    raise AssertionError(f"unexpected method {method}")


def _fetch(handler):
    from src.workers.orchestrator import _fetch_recent_block_transactions

    fake = _requests_module(handler)
    with patch.dict(sys.modules, {"requests": fake}):
        return _fetch_recent_block_transactions("http://node:8332", "u", "p"), fake


def test_the_best_block_transactions_are_returned():
    txs, fake = _fetch(_block_handler)

    assert txs == [_TX]
    # one round trip for the hash, one for the verbosity-2 block
    assert fake.calls == [("getbestblockhash", []), ("getblock", ["0000beef", 2])]


def test_a_block_with_no_transactions_yields_an_empty_list():
    def _handler(method, _params):
        if method == "getbestblockhash":
            return {"result": "0000beef"}
        return {"result": {"tx": None}}

    txs, _fake = _fetch(_handler)

    assert txs == []


def test_an_rpc_error_is_raised_rather_than_returned_as_data():
    def _handler(method, _params):
        if method == "getbestblockhash":
            return {"error": {"code": -8, "message": "out of range"}}
        raise AssertionError("should not reach getblock")

    with pytest.raises(RuntimeError, match="getbestblockhash failed"):
        _fetch(_handler)


def test_an_error_on_the_second_call_is_also_raised():
    def _handler(method, _params):
        if method == "getbestblockhash":
            return {"result": "0000beef"}
        return {"error": {"code": -5, "message": "block not found"}}

    with pytest.raises(RuntimeError, match="getblock failed"):
        _fetch(_handler)
