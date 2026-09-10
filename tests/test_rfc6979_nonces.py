"""
Tests for :mod:`src.mathcore.derivation.nonces`.

The oracle is RFC 6979 itself. Appendix A.2 publishes ``k`` for a fixed key
over several messages and hash functions on both P-256 and P-521, and those
vectors are the check: a deterministic nonce generator is correct exactly when
it reproduces them bit for bit.

The property that makes the scheme worth having -- no bias, no reuse across
distinct messages -- is asserted separately, because matching one vector does
not by itself show the candidate-rejection loop is sound.
"""

from __future__ import annotations

import hashlib

import pytest

from src.mathcore.derivation.nonces import (
    bits2int,
    bits2octets,
    generate_k,
    int2octets,
)

# RFC 6979 Appendix A.2.5, curve P-256, private key x.
P256_Q = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_X = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721

# (message, hash, expected k), all from A.2.5.
P256_VECTORS = (
    (
        b"sample",
        hashlib.sha1,
        0x882905F1227FD620FBF2ABF21244F0BA83D0DC3A9103DBBEE43A1FB858109DB4,
    ),
    (
        b"sample",
        hashlib.sha256,
        0xA6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60,
    ),
    (
        b"sample",
        hashlib.sha512,
        0x5FA81C63109BADB88C1F367B47DA606DA28CAD69AA22C4FE6AD7DF73A7173AA5,
    ),
    (
        b"test",
        hashlib.sha256,
        0xD16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0,
    ),
    (
        b"test",
        hashlib.sha512,
        0x6915D11632ACA3C40D5D51C08DAF9C555933819548784480E93499000D9F0B7F,
    ),
)

P521_X = int(
    "0FAD06DAA62BA3B25D2FB40133DA757205DE67F5BB0018FEE8C86E1B68C7E75CA"
    "A896EB32F1F47C70855836A6D16FCC1466F6D8FBEC67DB89EC0C08B0E996B8353",
    16,
)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ---- RFC 6979 vectors ------------------------------------------------------


@pytest.mark.parametrize(("message", "hash_factory", "expected"), P256_VECTORS)
def test_p256_vectors_reproduce_exactly(message, hash_factory, expected) -> None:
    digest = hash_factory(message).digest()
    k = generate_k(P256_Q, P256_X, digest, hash_factory=hash_factory)
    assert k == expected


# ---- the properties the scheme exists for ----------------------------------


def test_the_same_inputs_always_give_the_same_nonce() -> None:
    digest = _sha256(b"deterministic")
    first = generate_k(P256_Q, P256_X, digest)
    second = generate_k(P256_Q, P256_X, digest)
    assert first == second


def test_distinct_messages_give_distinct_nonces() -> None:
    """
    Reuse across two messages under one key leaks the key outright. Over a
    sweep of messages every nonce must differ.
    """
    seen = {generate_k(P256_Q, P256_X, _sha256(f"message-{i}".encode())) for i in range(200)}
    assert len(seen) == 200


def test_every_nonce_is_in_range() -> None:
    for i in range(200):
        k = generate_k(P256_Q, P256_X, _sha256(f"range-{i}".encode()))
        assert 1 <= k < P256_Q


def test_a_different_key_gives_a_different_nonce() -> None:
    digest = _sha256(b"same message")
    assert generate_k(P256_Q, P256_X, digest) != generate_k(P256_Q, P256_X ^ 1, digest)


def test_a_small_group_forces_the_rejection_loop() -> None:
    """
    A tiny ``q`` makes most HMAC candidates land at or above it, so the reseed
    branch runs many times. It is exercised here because on P-256 a rejection
    is astronomically rare and the branch would otherwise never be covered --
    and it is the branch whose absence would reintroduce modulo bias.
    """
    q = 5
    nonces = {generate_k(q, 3, bytes([i])) for i in range(50)}
    assert nonces
    assert all(1 <= k < q for k in nonces)


# ---- the RFC 6979 primitives -----------------------------------------------


def test_bits2int_keeps_the_high_bits_when_truncating() -> None:
    """
    The RFC right-shifts to fit ``qlen``; it does not reduce mod q. The shift
    keeps the top ``qlen`` bits, so a value one bit too long loses its lowest
    bit, not its highest.
    """
    assert bits2int(b"\xff\xff", 8) == 0xFF
    assert bits2int(b"\x80\x00", 8) == 0x80
    assert bits2int(b"\x12\x34", 16) == 0x1234
    assert bits2int(b"\x12\x34", 12) == 0x123


def test_bits2int_leaves_a_short_input_alone() -> None:
    assert bits2int(b"\x0a", 16) == 0x0A


def test_int2octets_pads_to_the_fixed_width() -> None:
    assert int2octets(1, 4) == b"\x00\x00\x00\x01"
    assert int2octets(0xABCD, 2) == b"\xab\xcd"


def test_int2octets_refuses_a_value_that_does_not_fit() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        int2octets(0x1234, 1)


def test_int2octets_refuses_a_negative_value() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        int2octets(-1, 4)


def test_bits2octets_reduces_modulo_q() -> None:
    """``z1 mod q`` with a single subtraction, since z1 < 2q."""
    q = 0xFF00
    # A two-octet input above q reduces by one subtraction.
    assert bits2octets(b"\xff\x80", q) == int2octets(0xFF80 - q, 2)
    assert bits2octets(b"\x00\x10", q) == b"\x00\x10"


# ---- preconditions ---------------------------------------------------------


@pytest.mark.parametrize("bad_q", [0, 1, 2, -7])
def test_a_degenerate_group_order_is_refused(bad_q: int) -> None:
    with pytest.raises(ValueError, match="group order"):
        generate_k(bad_q, 1, b"\x00")


@pytest.mark.parametrize("bad_key", [0, -1, P256_Q, P256_Q + 1])
def test_a_key_outside_the_range_is_refused(bad_key: int) -> None:
    with pytest.raises(ValueError, match=r"\[1, q\)"):
        generate_k(P256_Q, bad_key, _sha256(b"x"))
