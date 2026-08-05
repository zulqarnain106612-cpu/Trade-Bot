"""Tests for ECC analysis pipeline (secp256k1, ECDSA, Schnorr/Taproot, UTXO, zkSNARK)."""

from __future__ import annotations


class TestAddressClusterer:
    def test_empty_utxo_set(self) -> None:
        from src.ecc.secp256k1_cluster import AddressClusterer

        clusterer = AddressClusterer()
        result = clusterer.fit([])
        assert isinstance(result, list)
        assert result == []

    def test_flow_score_range_no_clusters(self) -> None:
        from src.ecc.secp256k1_cluster import AddressClusterer

        clusterer = AddressClusterer()
        clusters = clusterer.fit([])
        score = clusterer.flow_score(clusters)
        assert -1.0 <= score <= 1.0

    def test_single_utxo(self) -> None:
        from src.ecc.secp256k1_cluster import AddressClusterer

        clusterer = AddressClusterer()
        utxo = {
            "address": "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",  # pragma: allowlist secret
            "value_btc": 1.0,
            "age_days": 100,
        }
        result = clusterer.fit([utxo])
        assert isinstance(result, list)

    def test_cluster_worker_run_handles_rpc_error(self) -> None:
        from src.ecc.secp256k1_cluster import Secp256k1ClusterWorker

        worker = Secp256k1ClusterWorker(rpc_url="http://127.0.0.1:9999", rpc_user="x", rpc_pass="x")
        result = worker.run()
        # On RPC failure, must still return a ClusteringResult with valid fields
        assert hasattr(result, "flow_score")
        assert hasattr(result, "whale_count")
        assert -1.0 <= result.flow_score <= 1.0


class TestECDSAScanner:
    def test_scan_invalid_hex_returns_empty(self) -> None:
        from src.ecc.ecdsa_scan import ECDSAScanner

        scanner = ECDSAScanner()
        weaknesses = scanner.scan_transaction("invalid_hex")
        assert isinstance(weaknesses, list)
        assert weaknesses == []

    def test_weakness_score_starts_zero(self) -> None:
        from src.ecc.ecdsa_scan import ECDSAScanner

        scanner = ECDSAScanner()
        assert scanner.weakness_score == 0.0

    def test_extract_ecdsa_signatures_empty(self) -> None:
        from src.ecc.ecdsa_scan import extract_ecdsa_signatures

        sigs = extract_ecdsa_signatures("")
        assert sigs == []

    def test_nonce_reuse_detection_different_r(self) -> None:
        from src.ecc.ecdsa_scan import _extract_privkey_from_nonce_reuse

        # Different r values → no nonce reuse
        result = _extract_privkey_from_nonce_reuse(r=111, s1=222, s2=333, z1=444, z2=555)
        assert result is None


class TestSchnorrTaproot:
    def test_is_p2tr_output_false_for_empty(self) -> None:
        from src.ecc.schnorr_taproot import is_p2tr_output

        assert not is_p2tr_output(b"")

    def test_is_p2tr_output_true_for_witness_v1(self) -> None:
        from src.ecc.schnorr_taproot import is_p2tr_output

        # P2TR: OP_1 (0x51) + OP_PUSHBYTES_32 (0x20) + 32 bytes
        script = bytes([0x51, 0x20]) + b"\x00" * 32
        assert is_p2tr_output(script)

    def test_detect_musig2_empty(self) -> None:
        from src.ecc.schnorr_taproot import detect_musig2_cosigners

        result = detect_musig2_cosigners([])
        assert isinstance(result, list)

    def test_privacy_routing_range(self) -> None:
        from src.ecc.schnorr_taproot import estimate_privacy_routing

        score = estimate_privacy_routing([])
        assert 0.0 <= score <= 1.0

    def test_smart_money_divergence_range(self) -> None:
        from src.ecc.schnorr_taproot import compute_smart_money_divergence

        div = compute_smart_money_divergence([], retail_direction=0.5)
        assert -1.0 <= div <= 1.0

    def test_parse_taproot_block_empty(self) -> None:
        from src.ecc.schnorr_taproot import parse_taproot_block

        info = parse_taproot_block([])
        assert hasattr(info, "n_p2tr_outputs")
        assert info.n_p2tr_outputs == 0


class TestUTXOCurve:
    def test_empty_utxo_set(self) -> None:
        from src.ecc.utxo_curve import compute_hodler_index

        result = compute_hodler_index([])
        assert hasattr(result, "hodler_index")
        assert 0.0 <= result.hodler_index <= 1.0

    def test_single_old_utxo(self) -> None:
        from src.ecc.utxo_curve import compute_hodler_index

        utxos = [{"value_btc": 10.0, "age_days": 365 * 5}]
        result = compute_hodler_index(utxos)
        # Very old UTXO → high hodler index
        assert result.hodler_index > 0.5

    def test_single_fresh_utxo(self) -> None:
        from src.ecc.utxo_curve import compute_hodler_index

        utxos = [{"value_btc": 10.0, "age_days": 1}]
        result = compute_hodler_index(utxos)
        # Very new UTXO → low hodler index
        assert result.hodler_index < 0.2


class TestZkSnarkDetector:
    def test_detect_no_rpc(self) -> None:
        from src.ecc.zksnark_detect import ZkSnarkDetector

        detector = ZkSnarkDetector()
        result = detector.detect_mixing_flows(block_lookback=1)
        assert hasattr(result, "dark_pool_pressure")
        assert 0.0 <= result.dark_pool_pressure <= 1.0
