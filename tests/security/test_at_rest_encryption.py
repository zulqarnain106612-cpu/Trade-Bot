"""
SECR-009 — encrypted at rest, and provably decryptable.

The write path is exercised constantly and the read path once, badly, at the
worst possible moment. So this file spends most of its length on failures of
the read: the wrong key, the truncated file, the flipped byte, the blob from
an older format, the right key with the wrong associated data. Each of those
is a real restore that did not happen.

One deliberate design choice is tested here rather than merely commented:
every failure raises the *same* exception type. A caller mid-restore needs to
know the file is unusable, and giving them four catchable reasons invites a
loop that tries keys until one stops raising -- which is an oracle, and also
how somebody decrypts a backup with a key they should not have.
"""

from __future__ import annotations

import pytest

from src.security.at_rest import (
    KEY_BYTES,
    MAGIC,
    NONCE_BYTES,
    VERSION,
    DecryptionError,
    decrypt,
    encrypt,
    generate_key,
    parse,
    round_trip_check,
)

PLAINTEXT = b'{"balance_usd": 104233.11, "positions": ["BTC/USDT"]}'
AAD = b"backup/2026-09-12/positions"


@pytest.fixture
def key() -> bytes:
    return generate_key()


class TestTheRoundTrip:
    def test_plaintext_survives(self, key):
        assert decrypt(encrypt(PLAINTEXT, key), key) == PLAINTEXT

    def test_associated_data_survives(self, key):
        assert decrypt(encrypt(PLAINTEXT, key, AAD), key, AAD) == PLAINTEXT

    def test_empty_plaintext_is_allowed(self, key):
        # An empty table is a legitimate backup, and a special case here
        # would be a special case at 3am.
        assert decrypt(encrypt(b"", key), key) == b""

    def test_a_large_payload_survives(self, key):
        payload = b"x" * (2 * 1024 * 1024)
        assert decrypt(encrypt(payload, key), key) == payload

    def test_the_drill_passes(self, key):
        assert round_trip_check(key) is True


class TestTheCiphertextIsNotThePlaintext:
    def test_the_plaintext_does_not_appear_in_the_blob(self, key):
        assert PLAINTEXT not in encrypt(PLAINTEXT, key)

    def test_two_encryptions_of_one_plaintext_differ(self, key):
        # A fresh nonce each time. Identical ciphertexts would leak that two
        # backups had identical contents, which is itself information.
        assert encrypt(PLAINTEXT, key) != encrypt(PLAINTEXT, key)

    def test_the_envelope_is_identifiable(self, key):
        blob = encrypt(PLAINTEXT, key)
        assert blob.startswith(MAGIC)
        parsed = parse(blob)
        assert parsed.version == VERSION
        assert len(parsed.nonce) == NONCE_BYTES


class TestTheReadPathFailsSafely:
    def test_the_wrong_key_is_refused(self, key):
        blob = encrypt(PLAINTEXT, key)
        with pytest.raises(DecryptionError):
            decrypt(blob, generate_key())

    def test_a_flipped_byte_is_detected(self, key):
        blob = bytearray(encrypt(PLAINTEXT, key))
        blob[-1] ^= 0x01
        with pytest.raises(DecryptionError):
            decrypt(bytes(blob), key)

    def test_a_flipped_nonce_byte_is_detected(self, key):
        blob = bytearray(encrypt(PLAINTEXT, key))
        blob[len(MAGIC) + 1] ^= 0x01
        with pytest.raises(DecryptionError):
            decrypt(bytes(blob), key)

    def test_a_truncated_blob_is_refused(self, key):
        blob = encrypt(PLAINTEXT, key)
        with pytest.raises(DecryptionError):
            decrypt(blob[: len(blob) // 2], key)

    def test_an_empty_blob_is_refused(self, key):
        with pytest.raises(DecryptionError):
            decrypt(b"", key)

    def test_a_foreign_blob_is_refused(self, key):
        with pytest.raises(DecryptionError):
            decrypt(b"not our format at all, but long enough to reach the parser", key)

    def test_a_future_version_says_so(self, key):
        blob = bytearray(encrypt(PLAINTEXT, key))
        blob[len(MAGIC)] = VERSION + 1
        with pytest.raises(DecryptionError, match="version"):
            decrypt(bytes(blob), key)

    def test_mismatched_associated_data_is_refused(self, key):
        # This is what stops a blob being swapped for another one encrypted
        # under the same key: the name is authenticated even though it is not
        # secret.
        blob = encrypt(PLAINTEXT, key, AAD)
        with pytest.raises(DecryptionError):
            decrypt(blob, key, b"backup/2026-09-11/positions")

    def test_missing_associated_data_is_refused(self, key):
        blob = encrypt(PLAINTEXT, key, AAD)
        with pytest.raises(DecryptionError):
            decrypt(blob, key)

    def test_every_failure_raises_the_same_type(self, key):
        # Stated as a test because the alternative is so tempting: four
        # exception types would let a caller loop over keys until one worked.
        blob = encrypt(PLAINTEXT, key)
        bad_version = bytearray(blob)
        bad_version[len(MAGIC)] = 9
        cases = [
            (blob, generate_key()),
            (blob[:20], key),
            (b"", key),
            (bytes(bad_version), key),
        ]
        for payload, k in cases:
            with pytest.raises(DecryptionError):
                decrypt(payload, k)


class TestKeys:
    def test_a_generated_key_is_the_right_size(self):
        assert len(generate_key()) == KEY_BYTES

    def test_two_generated_keys_differ(self):
        assert generate_key() != generate_key()

    @pytest.mark.parametrize("bad", [b"", b"short", bytes(31), bytes(33), "a string", None])
    def test_a_wrong_sized_key_is_refused(self, bad):
        with pytest.raises((ValueError, TypeError)):
            encrypt(PLAINTEXT, bad)


class TestNoHomeGrownCrypto:
    """
    SECR-008's half of this module: the primitive is a library's, not ours.
    """

    def test_the_module_uses_the_cryptography_aead(self):
        import inspect

        from src.security import at_rest

        source = inspect.getsource(at_rest)
        assert "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" in source

    def test_it_defines_no_cipher_of_its_own(self):
        import inspect

        from src.security import at_rest

        source = inspect.getsource(at_rest).lower()
        for smell in ("def _sbox", "def _round_key", "def _feistel", "xor_stream"):
            assert smell not in source
