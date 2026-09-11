"""
BIP-32 hierarchical deterministic key derivation.

Owns the ``bip32-hd-derivation`` registry entry.

BIP-32 grows an unbounded tree of keys from a single seed, so one backed-up
secret controls every address a wallet ever uses. Each step is HMAC-SHA512 of
the chain code over the parent key and a child index, split into a scalar
tweak ``IL`` and the child's chain code; the child private key is
``parent + IL mod n`` and -- the feature that makes watch-only wallets possible
-- the child *public* key is ``parent_pubkey + IL*G``, derivable without the
private key.

That public-derivation path exists only for **non-hardened** indices (below
2^31). Hardened indices feed the private key into the HMAC instead of the
public key, which severs the parent-public-to-child-public link and is what
stops a leaked public key plus one child private key from unravelling the whole
branch. Asking to derive a hardened child from a public key is therefore a
category error, and this module raises rather than returning a wrong key.

Curve arithmetic is :mod:`src.mathcore.curves.secp256k1`; the scalar addition
and point tweak are the project's own, not a second implementation.

**Not constant time.** It manipulates private keys and must not be used to
derive live keys under a timing adversary; it is a reference for the derivation,
checked against the BIP-32 vectors.

References: BIP-32; SLIP-0010.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ..curves.secp256k1 import (
    CURVE_ORDER,
    GENERATOR,
    add,
    parse_point,
    scalar_multiply,
    serialize_point,
)

__all__ = [
    "HARDENED_OFFSET",
    "ExtendedKey",
    "derive_child_private",
    "derive_child_public",
    "master_key",
]

HARDENED_OFFSET = 1 << 31


@dataclass(frozen=True)
class ExtendedKey:
    """
    An extended key: a key plus the 32-byte chain code that seeds its children.

    ``key`` is a 32-byte scalar for a private extended key, or a 33-byte
    compressed point for a public one; ``is_private`` disambiguates. The chain
    code is the entropy that makes sibling keys independent -- two children of
    one parent share nothing derivable without it.
    """

    key: bytes
    chain_code: bytes
    is_private: bool

    def __post_init__(self) -> None:
        if len(self.chain_code) != 32:
            raise ValueError("chain code must be 32 bytes")
        expected = 32 if self.is_private else 33
        if len(self.key) != expected:
            raise ValueError(
                f"{'private' if self.is_private else 'public'} key must be "
                f"{expected} bytes, got {len(self.key)}"
            )


def master_key(seed: bytes) -> ExtendedKey:
    """
    The master private extended key from a seed, per BIP-32.

    ``HMAC-SHA512("Bitcoin seed", seed)`` splits into the master private key
    (left 32 bytes) and master chain code (right 32). A seed whose left half is
    zero or at least the group order has no valid key and is rejected, as the
    spec requires -- rather than producing an unusable key silently. Not
    constant time.
    """
    if len(seed) < 16:
        raise ValueError("seed should be at least 128 bits")
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    left, chain_code = digest[:32], digest[32:]
    scalar = int.from_bytes(left, "big")
    if scalar == 0 or scalar >= CURVE_ORDER:
        raise ValueError("seed produces an invalid master key; choose another")
    return ExtendedKey(key=left, chain_code=chain_code, is_private=True)


def _tweak(chain_code: bytes, data: bytes, index: int) -> tuple[int, bytes]:
    """HMAC-SHA512 the derivation input, returning ``(IL as int, child chain)``."""
    payload = data + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, payload, hashlib.sha512).digest()
    return int.from_bytes(digest[:32], "big"), digest[32:]


def derive_child_private(parent: ExtendedKey, index: int) -> ExtendedKey:
    """
    Derive the child private extended key at ``index``.

    Hardened when ``index >= HARDENED_OFFSET``: the HMAC is fed ``0x00 ||
    parent_key`` so the child does not depend on the parent public key.
    Non-hardened: it is fed the parent's compressed public key. The child scalar
    is ``(parent + IL) mod n``; an ``IL >= n`` or a resulting zero key is
    rejected as BIP-32 directs (the caller skips to the next index), rather than
    reduced into a silently different key. Not constant time.
    """
    if not parent.is_private:
        raise ValueError("a private child needs a private parent")
    if not 0 <= index < (1 << 32):
        raise ValueError(f"index must be a 32-bit integer, got {index}")

    parent_scalar = int.from_bytes(parent.key, "big")
    if index >= HARDENED_OFFSET:
        data = b"\x00" + parent.key
    else:
        parent_point = scalar_multiply(parent_scalar, GENERATOR)
        data = serialize_point(parent_point)
    il, child_chain = _tweak(parent.chain_code, data, index)
    if il >= CURVE_ORDER:
        raise ValueError("derived tweak is out of range; skip to the next index")
    child_scalar = (parent_scalar + il) % CURVE_ORDER
    if child_scalar == 0:
        raise ValueError("derived a zero child key; skip to the next index")
    return ExtendedKey(
        key=child_scalar.to_bytes(32, "big"), chain_code=child_chain, is_private=True
    )


def derive_child_public(parent: ExtendedKey, index: int) -> ExtendedKey:
    """
    Derive the child public extended key at a **non-hardened** ``index``.

    The child public key is ``parent_pubkey + IL*G``, computed without any
    private key -- the property that lets a watch-only wallet track an unbounded
    address tree. A hardened ``index`` is refused: hardened derivation needs the
    private key by design, and returning anything for it would be a wrong key
    dressed as a right one. Not constant time.
    """
    if parent.is_private:
        raise ValueError("derive_child_public expects a public parent key")
    if index >= HARDENED_OFFSET:
        raise ValueError(
            "cannot derive a hardened child from a public key; hardened "
            "derivation requires the private key by design"
        )
    if not 0 <= index < (1 << 32):
        raise ValueError(f"index must be a 32-bit integer, got {index}")

    parent_point = parse_point(parent.key)
    if parent_point is None:
        raise ValueError("parent public key does not decode to a curve point")
    il, child_chain = _tweak(parent.chain_code, parent.key, index)
    if il >= CURVE_ORDER:
        raise ValueError("derived tweak is out of range; skip to the next index")
    child_point = add(scalar_multiply(il, GENERATOR), parent_point)
    if child_point.is_infinity:
        raise ValueError("derived the point at infinity; skip to the next index")
    return ExtendedKey(key=serialize_point(child_point), chain_code=child_chain, is_private=False)


def _public_of(private: ExtendedKey) -> ExtendedKey:
    """The public extended key matching a private one. Not constant time."""
    point = scalar_multiply(int.from_bytes(private.key, "big"), GENERATOR)
    return ExtendedKey(key=serialize_point(point), chain_code=private.chain_code, is_private=False)
