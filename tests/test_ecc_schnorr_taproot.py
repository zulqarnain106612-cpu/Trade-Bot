"""Tests for src/ecc/schnorr_taproot.py -- Taproot/MuSig2 smart-money detection."""

from __future__ import annotations

from src.ecc.schnorr_taproot import (
    compute_smart_money_divergence,
    detect_musig2_cosigners,
    estimate_privacy_routing,
    is_p2tr_output,
    parse_taproot_block,
)


def _p2tr_script_hex(xonly_pubkey_hex: str) -> str:
    return "5120" + xonly_pubkey_hex  # OP_1 OP_PUSH32 <32 bytes>


def test_is_p2tr_output_true_for_valid_witness_program():
    script = bytes([0x51, 0x20]) + b"\x00" * 32
    assert is_p2tr_output(script) is True


def test_is_p2tr_output_false_for_wrong_length():
    assert is_p2tr_output(bytes([0x51, 0x20]) + b"\x00" * 10) is False


def test_is_p2tr_output_false_for_wrong_prefix():
    script = bytes([0x00, 0x14]) + b"\x00" * 32
    assert is_p2tr_output(script) is False


def test_detect_musig2_cosigners_groups_shared_prefix_key_path_spends():
    xonly = "aa" * 32  # 64 hex chars = 32 bytes
    inputs = [
        {"txinwitness": ["b" * 128], "prevout_scriptpubkey": _p2tr_script_hex(xonly)},
        {"txinwitness": ["c" * 128], "prevout_scriptpubkey": _p2tr_script_hex(xonly)},
    ]
    clusters = detect_musig2_cosigners(inputs)
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_detect_musig2_cosigners_ignores_singleton_groups():
    xonly = "bb" * 32
    inputs = [{"txinwitness": ["b" * 128], "prevout_scriptpubkey": _p2tr_script_hex(xonly)}]
    assert detect_musig2_cosigners(inputs) == []


def test_detect_musig2_cosigners_skips_empty_witness():
    assert detect_musig2_cosigners([{"txinwitness": []}]) == []


def test_detect_musig2_cosigners_skips_script_path_spends():
    # more than one witness item -> script-path spend, not key-path
    inputs = [{"txinwitness": ["a" * 64, "b" * 64], "prevout_scriptpubkey": "5120" + "aa" * 32}]
    assert detect_musig2_cosigners(inputs) == []


def test_detect_musig2_cosigners_skips_wrong_length_prevout_script():
    inputs = [{"txinwitness": ["b" * 128], "prevout_scriptpubkey": "5120aa"}]
    assert detect_musig2_cosigners(inputs) == []


def test_estimate_privacy_routing_empty_clusters_returns_zero():
    assert estimate_privacy_routing([]) == 0.0


def test_estimate_privacy_routing_scales_with_average_cluster_size():
    clusters = [["a", "b", "c", "d", "e"]]  # avg size 5 -> saturates at 1.0
    assert estimate_privacy_routing(clusters) == 1.0


def test_estimate_privacy_routing_below_saturation():
    clusters = [["a", "b"]]  # avg size 2 -> 2/5
    assert estimate_privacy_routing(clusters) == 2.0 / 5.0


def test_compute_smart_money_divergence_no_clusters_returns_zero():
    assert compute_smart_money_divergence([], retail_direction=0.5) == 0.0


def test_compute_smart_money_divergence_contrarian_to_retail():
    clusters = [["a", "b"]] * 10  # activity_signal saturates at 1.0
    divergence = compute_smart_money_divergence(clusters, retail_direction=0.8)
    assert divergence == -0.8


def test_parse_taproot_block_end_to_end():
    xonly = "cc" * 32
    tx = {
        "vin": [
            {
                "txinwitness": ["d" * 128],
                "prevout": {"scriptPubKey": {"hex": _p2tr_script_hex(xonly)}},
            },
            {
                "txinwitness": ["e" * 128],
                "prevout": {"scriptPubKey": {"hex": _p2tr_script_hex(xonly)}},
            },
            {
                # non-P2TR output -> skipped
                "txinwitness": ["f" * 128],
                "prevout": {"scriptPubKey": {"hex": "0014" + "00" * 20}},
            },
            {
                # script-path spend (multiple witness items)
                "txinwitness": ["g" * 64, "h" * 64],
                "prevout": {"scriptPubKey": {"hex": _p2tr_script_hex("dd" * 32)}},
            },
        ]
    }
    info = parse_taproot_block([tx])
    assert info.p2tr_input_count == 3  # two key-path + one script-path P2TR
    assert info.key_path_spends == 2
    assert info.script_path_spends == 1
    assert info.musig2_count == 1  # the two shared-prefix key-path inputs


def test_parse_taproot_block_skips_invalid_hex():
    tx = {"vin": [{"prevout": {"scriptPubKey": {"hex": "not-hex"}}}]}
    info = parse_taproot_block([tx])
    assert info.p2tr_input_count == 0


def test_parse_taproot_block_no_transactions():
    info = parse_taproot_block([])
    assert info.p2tr_input_count == 0
    assert info.musig2_count == 0
