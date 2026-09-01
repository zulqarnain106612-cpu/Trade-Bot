"""Tests for src/ecc/secp256k1_cluster.py -- UTXO clustering + whale flow score.

graphsense-lib is an optional, "soft" dependency not installed in CI, so
AddressClusterer naturally falls back to the union-find path here; a fake
module via sys.modules also covers the graphsense-available path.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from src.ecc.secp256k1_cluster import (
    AddressClusterer,
    ClusterInfo,
    Secp256k1ClusterWorker,
)


def test_cluster_info_net_flow():
    c = ClusterInfo(cluster_id=1, total_btc=10.0, addresses=["a"], inflow_btc=5.0, outflow_btc=2.0)
    assert c.net_flow_btc == 3.0


def test_cluster_info_is_whale_true_and_false():
    whale = ClusterInfo(cluster_id=1, total_btc=200.0, addresses=["a"])
    minnow = ClusterInfo(cluster_id=2, total_btc=1.0, addresses=["b"])
    assert whale.is_whale is True
    assert minnow.is_whale is False


def test_clusterer_falls_back_when_graphsense_missing():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    assert clusterer._graphsense_available is False


def test_clusterer_uses_graphsense_when_available():
    fake_module = MagicMock()
    fake_instance = MagicMock()
    fake_module.AddressClusterer.return_value = fake_instance
    with patch.dict(sys.modules, {"graphsense": fake_module}):
        clusterer = AddressClusterer()
    assert clusterer._graphsense_available is True
    fake_module.AddressClusterer.assert_called_once_with(curve="secp256k1")


def test_fit_empty_utxos_returns_empty_list():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    assert clusterer.fit([]) == []


def test_fit_union_find_groups_addresses_by_shared_transaction():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    utxos = [
        {"txid": "tx1", "address": "addrA", "amount": 1.0},
        {"txid": "tx1", "address": "addrB", "amount": 2.0},
        {"txid": "tx2", "address": "addrC", "value": 3.0},
    ]
    clusters = clusterer.fit(utxos)
    # addrA and addrB share tx1 -> one cluster; addrC is separate
    sizes = sorted(len(c.addresses) for c in clusters)
    assert sizes == [1, 2]
    combined = next(c for c in clusters if len(c.addresses) == 2)
    assert combined.total_btc == 3.0


def test_fit_union_find_utxo_without_txid_forms_singleton_cluster():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    # No txid means nothing to union with, but the address is still real --
    # it becomes its own singleton cluster rather than being dropped.
    utxos = [{"txid": "", "address": "a", "amount": 1.0}]
    result = clusterer.fit(utxos)
    assert len(result) == 1
    assert result[0].addresses == ["a"]


def test_fit_union_find_drops_utxos_with_no_address():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    utxos = [{"txid": "tx1", "address": ""}]
    assert clusterer.fit(utxos) == []


def test_fit_graphsense_success_path():
    fake_module = MagicMock()
    fake_instance = MagicMock()
    fake_module.AddressClusterer.return_value = fake_instance
    raw_cluster = MagicMock(total_btc=42.0, addresses=["a", "b"])
    fake_instance.fit.return_value = [raw_cluster]

    with patch.dict(sys.modules, {"graphsense": fake_module}):
        clusterer = AddressClusterer()
        result = clusterer.fit([{"txid": "tx1", "address": "a"}])

    assert len(result) == 1
    assert result[0].total_btc == 42.0
    assert result[0].addresses == ["a", "b"]


def test_fit_graphsense_failure_falls_back_to_union_find():
    fake_module = MagicMock()
    fake_instance = MagicMock()
    fake_module.AddressClusterer.return_value = fake_instance
    fake_instance.fit.side_effect = RuntimeError("gs down")

    with patch.dict(sys.modules, {"graphsense": fake_module}):
        clusterer = AddressClusterer()
        result = clusterer.fit([{"txid": "tx1", "address": "a", "amount": 1.0}])

    assert len(result) == 1
    assert result[0].addresses == ["a"]


def test_flow_score_no_whales_returns_zero():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    minnow = ClusterInfo(cluster_id=1, total_btc=1.0, addresses=["a"])
    assert clusterer.flow_score([minnow]) == 0.0


def test_flow_score_positive_for_net_accumulation():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    whale = ClusterInfo(
        cluster_id=1, total_btc=200.0, addresses=["a"], inflow_btc=100.0, outflow_btc=0.0
    )
    score = clusterer.flow_score([whale])
    assert score == 0.5  # net_flow_btc (100) / total_btc (200)


def test_flow_score_clamped_to_minus_one_for_full_distribution():
    with patch.dict(sys.modules, {"graphsense": None}):
        clusterer = AddressClusterer()
    whale = ClusterInfo(
        cluster_id=1, total_btc=200.0, addresses=["a"], inflow_btc=0.0, outflow_btc=500.0
    )
    score = clusterer.flow_score([whale])
    assert score == -1.0


def test_worker_run_success_path():
    worker = Secp256k1ClusterWorker(rpc_url="http://node", rpc_user="u", rpc_pass="p")
    fake_response = MagicMock()
    fake_response.json.return_value = {"result": [{"txid": "tx1", "address": "a", "amount": 150.0}]}
    with (
        patch.dict(sys.modules, {"graphsense": None}),
        patch("requests.post", return_value=fake_response) as mock_post,
    ):
        result = worker.run()

    mock_post.assert_called_once()
    assert result.whale_count == 1
    assert result.total_whale_btc == 150.0
    assert result.utxos == [{"txid": "tx1", "address": "a", "amount": 150.0}]


def test_worker_run_rpc_failure_returns_zeroed_result():
    worker = Secp256k1ClusterWorker(rpc_url="http://node", rpc_user="u", rpc_pass="p")
    with (
        patch.dict(sys.modules, {"graphsense": None}),
        patch("requests.post", side_effect=ConnectionError("no route")),
    ):
        result = worker.run()

    assert result.flow_score == 0.0
    assert result.whale_count == 0
    assert result.total_whale_btc == 0.0
    assert result.clusters == []
    assert result.utxos == []
