"""
Tests for :mod:`src.mathcore.derivation.bip32`.

The oracle is BIP-32's own test vector 1: seed
000102030405060708090a0b0c0d0e0f, whose master key, chain code, and m/0'
public key are published. Those are checked byte-for-byte -- a derivation with
any defect cannot reproduce them. The structural guarantees (public derivation
matches private-then-public, hardened children refuse public derivation) are
checked as properties.
"""

from __future__ import annotations

import pytest

from src.mathcore.derivation.bip32 import (
    HARDENED_OFFSET,
    ExtendedKey,
    derive_child_private,
    derive_child_public,
    master_key,
)
from src.mathcore.derivation.bip32 import _public_of as public_of

VECTOR_1_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
MASTER_PRIV = "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"
MASTER_CHAIN = "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508"
# m/0' -- published public key and chain code.
M0H_PUB = "035a784662a4a20a65bf6aab9ae98a6c068a81c52e4b032c0fb5400c706cfccc56"
M0H_CHAIN = "47fdacbd0f1097043b78c63c20c34ef4ed9a111d980047ad16282c7ae6236141"


# ---- BIP-32 test vector 1 (known answer) -----------------------------------


def test_master_key_matches_the_published_vector() -> None:
    m = master_key(VECTOR_1_SEED)
    assert m.key.hex() == MASTER_PRIV
    assert m.chain_code.hex() == MASTER_CHAIN
    assert m.is_private


def test_first_hardened_child_matches_the_published_public_key() -> None:
    """
    m/0' -- checked via its public key and chain code, which the spec
    publishes; matching both fixes the private key too.
    """
    m = master_key(VECTOR_1_SEED)
    child = derive_child_private(m, HARDENED_OFFSET)
    assert public_of(child).key.hex() == M0H_PUB
    assert child.chain_code.hex() == M0H_CHAIN


# ---- structural guarantees -------------------------------------------------


def test_public_derivation_matches_private_then_public() -> None:
    """The watch-only property: deriving a non-hardened child public key from
    the parent public key gives the same key as deriving privately then taking
    the public key."""
    m = master_key(VECTOR_1_SEED)
    for index in (0, 1, 42, HARDENED_OFFSET - 1):
        via_private = public_of(derive_child_private(m, index))
        via_public = derive_child_public(public_of(m), index)
        assert via_private.key == via_public.key
        assert via_private.chain_code == via_public.chain_code


def test_a_hardened_child_cannot_be_derived_from_a_public_key() -> None:
    m = master_key(VECTOR_1_SEED)
    with pytest.raises(ValueError, match="hardened"):
        derive_child_public(public_of(m), HARDENED_OFFSET)


def test_hardened_and_non_hardened_children_differ() -> None:
    m = master_key(VECTOR_1_SEED)
    soft = derive_child_private(m, 0)
    hard = derive_child_private(m, HARDENED_OFFSET)
    assert soft.key != hard.key


def test_derivation_is_deterministic() -> None:
    m = master_key(VECTOR_1_SEED)
    assert derive_child_private(m, 5).key == derive_child_private(m, 5).key


def test_a_grandchild_derives() -> None:
    m = master_key(VECTOR_1_SEED)
    child = derive_child_private(m, HARDENED_OFFSET + 1)
    grandchild = derive_child_private(child, 2)
    assert grandchild.is_private
    assert len(grandchild.key) == 32


# ---- validation ------------------------------------------------------------


def test_a_short_seed_is_refused() -> None:
    with pytest.raises(ValueError, match="128 bits"):
        master_key(b"\x00" * 8)


def test_private_derivation_needs_a_private_parent() -> None:
    m = master_key(VECTOR_1_SEED)
    with pytest.raises(ValueError, match="private parent"):
        derive_child_private(public_of(m), 0)


def test_public_derivation_needs_a_public_parent() -> None:
    m = master_key(VECTOR_1_SEED)
    with pytest.raises(ValueError, match="public parent"):
        derive_child_public(m, 0)


@pytest.mark.parametrize("bad", [-1, 1 << 32])
def test_an_out_of_range_index_is_refused(bad: int) -> None:
    m = master_key(VECTOR_1_SEED)
    with pytest.raises(ValueError, match="32-bit"):
        derive_child_private(m, bad)


def test_extended_key_validates_its_lengths() -> None:
    with pytest.raises(ValueError, match="chain code"):
        ExtendedKey(key=b"\x01" * 32, chain_code=b"\x00" * 16, is_private=True)
    with pytest.raises(ValueError, match="private key must be 32"):
        ExtendedKey(key=b"\x01" * 33, chain_code=b"\x00" * 32, is_private=True)
    with pytest.raises(ValueError, match="public key must be 33"):
        ExtendedKey(key=b"\x01" * 32, chain_code=b"\x00" * 32, is_private=False)


def test_public_derivation_rejects_an_undecodable_parent() -> None:
    # x = 2^256 - 1 is above the field prime, so no curve point has it.
    bad = ExtendedKey(key=b"\x03" + b"\xff" * 32, chain_code=b"\x00" * 32, is_private=False)
    with pytest.raises(ValueError, match="decode"):
        derive_child_public(bad, 0)


def test_public_derivation_rejects_a_negative_index() -> None:
    m = master_key(VECTOR_1_SEED)
    with pytest.raises(ValueError, match="32-bit"):
        derive_child_public(public_of(m), -1)


# ---- the BIP-32-mandated guards for degenerate HMAC output -----------------
#
# These raise on a tweak >= n or a resulting zero/infinity key. Each has
# probability about 2**-128 from a real HMAC, so they cannot be reached by any
# input a test could supply; the tweak is forced instead, which is the only
# honest way to exercise a guard the standard requires but chance never trips.


def test_master_key_rejects_an_out_of_range_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.mathcore.derivation.bip32 as bip32
    from src.mathcore.curves.secp256k1 import CURVE_ORDER

    class _FakeHMAC:
        def __init__(self, digest: bytes) -> None:
            self._d = digest

        def digest(self) -> bytes:
            return self._d

    over = CURVE_ORDER.to_bytes(32, "big") + b"\x11" * 32
    monkeypatch.setattr(bip32.hmac, "new", lambda *a, **k: _FakeHMAC(over))
    with pytest.raises(ValueError, match="invalid master key"):
        master_key(VECTOR_1_SEED)


def test_private_derivation_rejects_a_tweak_at_or_above_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.mathcore.derivation.bip32 as bip32
    from src.mathcore.curves.secp256k1 import CURVE_ORDER

    m = master_key(VECTOR_1_SEED)
    monkeypatch.setattr(bip32, "_tweak", lambda *a: (CURVE_ORDER, b"\x00" * 32))
    with pytest.raises(ValueError, match="out of range"):
        derive_child_private(m, 0)


def test_private_derivation_rejects_a_zero_child(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.mathcore.derivation.bip32 as bip32
    from src.mathcore.curves.secp256k1 import CURVE_ORDER

    m = master_key(VECTOR_1_SEED)
    parent_scalar = int.from_bytes(m.key, "big")
    forced = (CURVE_ORDER - parent_scalar) % CURVE_ORDER  # makes parent + il == 0
    monkeypatch.setattr(bip32, "_tweak", lambda *a: (forced, b"\x00" * 32))
    with pytest.raises(ValueError, match="zero child"):
        derive_child_private(m, 0)


def test_public_derivation_rejects_a_tweak_at_or_above_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.mathcore.derivation.bip32 as bip32
    from src.mathcore.curves.secp256k1 import CURVE_ORDER

    m = master_key(VECTOR_1_SEED)
    monkeypatch.setattr(bip32, "_tweak", lambda *a: (CURVE_ORDER, b"\x00" * 32))
    with pytest.raises(ValueError, match="out of range"):
        derive_child_public(public_of(m), 0)


def test_public_derivation_rejects_a_child_at_infinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.mathcore.derivation.bip32 as bip32
    from src.mathcore.curves.secp256k1 import CURVE_ORDER, GENERATOR, serialize_point

    # Parent public key = G, forced tweak = n - 1, so child = (n-1)G + G = O.
    parent = ExtendedKey(key=serialize_point(GENERATOR), chain_code=b"\x00" * 32, is_private=False)
    monkeypatch.setattr(bip32, "_tweak", lambda *a: (CURVE_ORDER - 1, b"\x00" * 32))
    with pytest.raises(ValueError, match="infinity"):
        derive_child_public(parent, 0)
