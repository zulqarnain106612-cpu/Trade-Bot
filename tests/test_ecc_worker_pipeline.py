"""Tests for the ECC worker pipeline.

The worker's docstring always claimed five analyses; only two were wired. These
cover the other three and the per-analysis isolation that keeps one dead data
source from zeroing the whole feature set.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.ecc.secp256k1_cluster import ClusteringResult
from src.workers.orchestrator import (
    _ECC_NEUTRAL_FEATURES,
    _fetch_recent_block_transactions,
    _run_ecc_cycle,
    _utxos_with_ages,
)


def _utxo(age_days: float, amount: float = 1.0) -> dict:
    return {"address": f"addr{age_days}", "amount": amount, "confirmations": int(age_days * 144)}


def _cluster_result(utxos: list[dict] | None = None) -> ClusteringResult:
    return ClusteringResult(
        flow_score=0.5,
        whale_count=2,
        total_whale_btc=100.0,
        utxos=utxos or [],
    )


def _cycle(
    clusterer: Any = None,
    zk_detector: Any = None,
    ecdsa_scanner: Any = None,
    transactions: list[dict] | None = None,
) -> dict:
    clusterer = clusterer or MagicMock(**{"run.return_value": _cluster_result()})
    zk_detector = zk_detector or MagicMock(
        **{
            "detect_mixing_flows.return_value": MagicMock(
                dark_pool_pressure=0.3, tornado_deposits_detected=4
            )
        }
    )
    ecdsa_scanner = ecdsa_scanner or MagicMock(**{"scan_transaction.return_value": []})
    with patch(
        "src.workers.orchestrator._fetch_recent_block_transactions",
        return_value=transactions if transactions is not None else [],
    ):
        return _run_ecc_cycle(
            clusterer=clusterer,
            zk_detector=zk_detector,
            ecdsa_scanner=ecdsa_scanner,
            rpc_url="http://node",
            rpc_user="u",
            rpc_pass="p",
        )


class TestFeatureContract:
    def test_result_always_carries_every_declared_key(self) -> None:
        assert set(_cycle()) == set(_ECC_NEUTRAL_FEATURES)

    def test_total_failure_still_returns_the_neutral_set(self) -> None:
        result = _cycle(
            clusterer=MagicMock(**{"run.side_effect": RuntimeError("node down")}),
            zk_detector=MagicMock(**{"detect_mixing_flows.side_effect": RuntimeError("eth down")}),
        )
        assert result == _ECC_NEUTRAL_FEATURES

    def test_cluster_and_zksnark_features_are_reported(self) -> None:
        result = _cycle()
        assert result["cluster_flow_score"] == 0.5
        assert result["whale_count"] == 2
        assert result["dark_pool_pressure"] == 0.3
        assert result["tornado_deposits"] == 4


class TestIsolation:
    def test_a_dead_eth_node_does_not_discard_bitcoin_features(self) -> None:
        result = _cycle(
            zk_detector=MagicMock(**{"detect_mixing_flows.side_effect": RuntimeError("eth down")})
        )
        assert result["cluster_flow_score"] == 0.5
        assert result["dark_pool_pressure"] == 0.0

    def test_a_dead_btc_node_does_not_discard_eth_features(self) -> None:
        result = _cycle(clusterer=MagicMock(**{"run.side_effect": RuntimeError("btc down")}))
        assert result["dark_pool_pressure"] == 0.3
        assert result["cluster_flow_score"] == 0.0

    def test_a_failed_block_fetch_leaves_the_rest_intact(self) -> None:
        clusterer = MagicMock(**{"run.return_value": _cluster_result()})
        zk = MagicMock(
            **{
                "detect_mixing_flows.return_value": MagicMock(
                    dark_pool_pressure=0.3, tornado_deposits_detected=4
                )
            }
        )
        with patch(
            "src.workers.orchestrator._fetch_recent_block_transactions",
            side_effect=RuntimeError("rpc down"),
        ):
            result = _run_ecc_cycle(
                clusterer=clusterer,
                zk_detector=zk,
                ecdsa_scanner=MagicMock(),
                rpc_url="http://node",
                rpc_user="u",
                rpc_pass="p",
            )
        assert result["cluster_flow_score"] == 0.5
        assert result["p2tr_input_count"] == 0


class TestUTXOCurve:
    def test_hodler_features_are_derived_from_the_clusterer_utxos(self) -> None:
        utxos = [_utxo(500), _utxo(600), _utxo(700)]
        result = _cycle(clusterer=MagicMock(**{"run.return_value": _cluster_result(utxos)}))
        assert result["mean_age_days"] > 0.0
        assert result["aged_supply_pct"] > 0.0

    def test_the_utxo_set_is_not_refetched_from_the_node(self) -> None:
        clusterer = MagicMock(**{"run.return_value": _cluster_result([_utxo(500)])})
        _cycle(clusterer=clusterer)
        clusterer.run.assert_called_once()

    def test_an_empty_utxo_set_leaves_the_curve_features_neutral(self) -> None:
        result = _cycle(clusterer=MagicMock(**{"run.return_value": _cluster_result([])}))
        assert result["hodler_index"] == 0.0
        assert result["mean_age_days"] == 0.0


class TestUTXOAgeConversion:
    def test_confirmations_become_a_creation_timestamp(self) -> None:
        [converted] = _utxos_with_ages([_utxo(10)])
        age_days = (time.time() - converted["timestamp"]) / 86400.0
        assert age_days == pytest.approx(10.0, abs=0.1)

    def test_an_existing_timestamp_is_preserved(self) -> None:
        original = {"amount": 1.0, "timestamp": 123.0, "confirmations": 99999}
        assert _utxos_with_ages([original]) == [original]

    def test_a_missing_confirmation_count_dates_the_utxo_to_now(self) -> None:
        [converted] = _utxos_with_ages([{"amount": 1.0}])
        assert converted["timestamp"] == pytest.approx(time.time(), abs=5)

    def test_conversion_does_not_mutate_the_input(self) -> None:
        original = {"amount": 1.0, "confirmations": 144}
        _utxos_with_ages([original])
        assert "timestamp" not in original


class TestECDSAScan:
    def test_transactions_without_hex_are_skipped(self) -> None:
        scanner = MagicMock(**{"scan_transaction.return_value": []})
        _cycle(ecdsa_scanner=scanner, transactions=[{"txid": "a"}, {"txid": "b"}])
        scanner.scan_transaction.assert_not_called()

    def test_weaknesses_and_recovered_keys_are_counted(self) -> None:
        scanner = MagicMock(
            **{
                "scan_transaction.return_value": [
                    MagicMock(privkey_extracted=True),
                    MagicMock(privkey_extracted=False),
                ]
            }
        )
        result = _cycle(ecdsa_scanner=scanner, transactions=[{"hex": "00"}])
        assert result["ecdsa_weaknesses"] == 2
        assert result["ecdsa_keys_recovered"] == 1

    def test_a_scanner_fault_does_not_abort_the_cycle(self) -> None:
        scanner = MagicMock(**{"scan_transaction.side_effect": ValueError("bad der")})
        result = _cycle(ecdsa_scanner=scanner, transactions=[{"hex": "00"}])
        assert result["cluster_flow_score"] == 0.5
        assert result["ecdsa_weaknesses"] == 0


class TestBlockFetch:
    def _response(self, results: list) -> Any:
        responses = [MagicMock(**{"json.return_value": {"result": r}}) for r in results]
        return responses

    def test_best_block_transactions_are_returned(self) -> None:
        with patch("requests.post", side_effect=self._response(["hash", {"tx": [{"txid": "a"}]}])):
            assert _fetch_recent_block_transactions("http://n", "u", "p") == [{"txid": "a"}]

    def test_a_block_without_transactions_yields_an_empty_list(self) -> None:
        with patch("requests.post", side_effect=self._response(["hash", {}])):
            assert _fetch_recent_block_transactions("http://n", "u", "p") == []

    def test_an_rpc_error_is_raised_not_swallowed(self) -> None:
        resp = MagicMock(**{"json.return_value": {"error": {"code": -8, "message": "nope"}}})
        with patch("requests.post", return_value=resp):
            with pytest.raises(RuntimeError, match="getbestblockhash failed"):
                _fetch_recent_block_transactions("http://n", "u", "p")
