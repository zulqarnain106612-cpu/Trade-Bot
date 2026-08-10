"""Coverage for the ECDSA weakness scanner.

The scanner is what turns raw block transactions into the ecdsa_weakness
signal the RL state consumes, so its parsing and key-recovery paths matter.
"""

from __future__ import annotations

from src.ecc.ecdsa_scan import (
    _SECP256K1_N,
    ECDSAScanner,
    _extract_privkey_from_nonce_reuse,
    _parse_der_signature,
    extract_ecdsa_signatures,
)


def _der(r: int, s: int) -> bytes:
    """Build a minimal DER signature for r and s."""
    rb = r.to_bytes((r.bit_length() + 7) // 8 or 1, "big")
    sb = s.to_bytes((s.bit_length() + 7) // 8 or 1, "big")
    body = b"\x02" + bytes([len(rb)]) + rb + b"\x02" + bytes([len(sb)]) + sb
    return b"\x30" + bytes([len(body)]) + body


def _tx_with(sigs: list[bytes]) -> str:
    """Wrap DER signatures in a byte blob, each followed by a sighash + pubkey."""
    out = b"\x01\x00\x00\x00"
    for sig in sigs:
        out += sig + b"\x01" + b"\x02" + b"\xab" * 32
    return (out + b"\x00" * 8).hex()


class TestParseDER:
    def test_a_well_formed_signature_round_trips(self) -> None:
        assert _parse_der_signature(_der(0x1234, 0x5678)) == (0x1234, 0x5678)

    def test_too_short_is_rejected(self) -> None:
        assert _parse_der_signature(b"\x30\x02\x02") is None

    def test_a_wrong_leading_byte_is_rejected(self) -> None:
        assert _parse_der_signature(b"\x31" + b"\x00" * 16) is None

    def test_a_missing_r_marker_is_rejected(self) -> None:
        assert _parse_der_signature(b"\x30\x0a\x03\x02\x01\x02\x02\x02\x03\x04") is None

    def test_a_missing_s_marker_is_rejected(self) -> None:
        der = bytearray(_der(0x1234, 0x5678))
        der[4 + der[3]] = 0x03  # corrupt the second 0x02 marker
        assert _parse_der_signature(bytes(der)) is None

    def test_a_truncated_body_is_rejected(self) -> None:
        assert _parse_der_signature(_der(0x1234, 0x5678)[:-3]) is None


class TestExtractSignatures:
    def test_a_signature_followed_by_a_compressed_pubkey_is_found(self) -> None:
        found = extract_ecdsa_signatures(_tx_with([_der(0x11, 0x22)]))
        assert len(found) == 1
        r, s, pubkey, txid = found[0]
        assert (r, s) == (0x11, 0x22)
        assert len(pubkey) == 33
        assert len(txid) == 64

    def test_several_signatures_are_all_found(self) -> None:
        found = extract_ecdsa_signatures(_tx_with([_der(0x11, 0x22), _der(0x33, 0x44)]))
        assert [(r, s) for r, s, _, _ in found] == [(0x11, 0x22), (0x33, 0x44)]

    def test_a_blob_with_no_signature_yields_nothing(self) -> None:
        assert extract_ecdsa_signatures((b"\x00" * 64).hex()) == []

    def test_invalid_hex_is_swallowed(self) -> None:
        assert extract_ecdsa_signatures("not hex at all") == []

    def test_an_empty_transaction_yields_nothing(self) -> None:
        assert extract_ecdsa_signatures("") == []


class TestPrivkeyRecovery:
    def test_identical_s_values_cannot_recover_a_key(self) -> None:
        assert _extract_privkey_from_nonce_reuse(r=5, s1=7, s2=7, z1=1, z2=2) is None

    def test_reuse_with_distinct_s_recovers_a_key(self) -> None:
        key = _extract_privkey_from_nonce_reuse(r=5, s1=7, s2=9, z1=1, z2=2)
        assert key is not None
        assert 0 < key < _SECP256K1_N

    def test_equal_z_values_give_a_zero_nonce_and_no_key(self) -> None:
        assert _extract_privkey_from_nonce_reuse(r=5, s1=7, s2=9, z1=3, z2=3) is None

    def test_a_non_invertible_r_yields_no_key(self) -> None:
        assert _extract_privkey_from_nonce_reuse(r=0, s1=7, s2=9, z1=1, z2=2) is None

    def test_recovery_is_consistent_for_the_same_inputs(self) -> None:
        args = {"r": 11, "s1": 13, "s2": 17, "z1": 19, "z2": 23}
        first = _extract_privkey_from_nonce_reuse(**args)
        assert first == _extract_privkey_from_nonce_reuse(**args)


class TestScanner:
    def test_a_single_signature_raises_no_alarm(self) -> None:
        scanner = ECDSAScanner()
        assert scanner.scan_transaction(_tx_with([_der(0x11, 0x22)])) == []
        assert scanner.weakness_score == 0.0

    def test_a_reused_r_across_transactions_is_flagged(self) -> None:
        scanner = ECDSAScanner()
        scanner.scan_transaction(_tx_with([_der(0x99, 0x11)]))
        found = scanner.scan_transaction(_tx_with([_der(0x99, 0x22)]))
        assert len(found) == 1
        assert found[0].r_value == 0x99
        assert scanner.weakness_score >= 0.7

    def test_a_recovered_key_scores_higher_than_a_bare_collision(self) -> None:
        scanner = ECDSAScanner()
        scanner.scan_transaction(_tx_with([_der(0x99, 0x11)]))
        [weakness] = scanner.scan_transaction(_tx_with([_der(0x99, 0x22)]))
        expected = 0.95 if weakness.privkey_extracted else 0.7
        assert weakness.risk_score == expected

    def test_distinct_r_values_never_collide(self) -> None:
        scanner = ECDSAScanner()
        scanner.scan_transaction(_tx_with([_der(0x01, 0x11)]))
        assert scanner.scan_transaction(_tx_with([_der(0x02, 0x22)])) == []

    def test_a_transaction_with_no_signatures_is_a_no_op(self) -> None:
        scanner = ECDSAScanner()
        assert scanner.scan_transaction((b"\x00" * 64).hex()) == []

    def test_the_registry_is_pruned_once_it_exceeds_the_cap(self) -> None:
        scanner = ECDSAScanner()
        for r in range(50):
            scanner._r_registry[r].append((1, "pk", "tx", 0))
        scanner.clear_old_entries(max_registry_size=10)
        assert len(scanner._r_registry) == 45

    def test_pruning_below_the_cap_keeps_everything(self) -> None:
        scanner = ECDSAScanner()
        for r in range(5):
            scanner._r_registry[r].append((1, "pk", "tx", 0))
        scanner.clear_old_entries(max_registry_size=100)
        assert len(scanner._r_registry) == 5
