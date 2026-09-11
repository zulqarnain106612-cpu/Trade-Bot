"""
RFC 6979 deterministic ECDSA/DSA nonce generation.

Owns the ``rfc6979-deterministic-nonces`` registry entry.

The nonce ``k`` in an ECDSA signature must be secret and it must never repeat
for two different messages under one key. A random ``k`` from a weak RNG has
sunk real wallets -- reuse leaks the key outright, and even a few biased bits
leak it to a lattice attack. RFC 6979 removes the RNG from the signing path
entirely: ``k`` is an HMAC-DRBG output seeded by the private key and the
message hash, so it is reproducible, never repeats across distinct messages,
and has no bias for :mod:`mathcore.lattice.hnp` to exploit. This module is the
generator that closes the attack surface the lattice modules attack.

**This derives a secret.** Unlike the rest of :mod:`mathcore`, whose inputs are
public, ``k`` and the private key here are exactly the values a timing or
memory adversary wants. The implementation follows the RFC's structure so it
can be checked against the RFC's test vectors, and HMAC itself is
constant-time in the stdlib -- but the surrounding integer conversions and the
rejection loop are not, and this is a reference for correctness, not a
side-channel-hardened signer. A production signer should use a vetted library;
what this module buys is a checkable oracle and the closing of the *bias*
class, which is a different threat from a timing leak.

References: RFC 6979 sec. 3.2 and app. A.2; FIPS 186-4; SEC 1 v2.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

__all__ = [
    "bits2int",
    "bits2octets",
    "generate_k",
    "int2octets",
]

HashFactory = Callable[[], "hashlib._Hash"]


def _qlen_rlen(q: int) -> tuple[int, int]:
    """``(qlen, rlen)``: the bit length of ``q`` and its length in whole octets."""
    qlen = q.bit_length()
    rlen = (qlen + 7) // 8
    return qlen, rlen


def bits2int(data: bytes, qlen: int) -> int:
    """
    RFC 6979 section 2.3.2: interpret ``data`` as an integer of at most ``qlen``
    bits.

    The big-endian value of ``data`` is taken, then right-shifted if it carries
    more than ``qlen`` bits so that the result fits. The shift, rather than a
    modular reduction, is deliberate and is what the RFC specifies: it keeps
    the high bits, which is what makes the derivation reproducible against the
    published vectors. Reducing modulo ``q`` here instead is a common and
    silent bug. Not constant time.
    """
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - qlen
    if excess > 0:
        value >>= excess
    return value


def int2octets(value: int, rlen: int) -> bytes:
    """
    RFC 6979 section 2.3.3: encode ``value`` as exactly ``rlen`` octets.

    ``value`` is expected already reduced into ``[0, q)`` by the caller; an
    out-of-range value cannot fit ``rlen`` octets and is rejected rather than
    truncated, because a silent truncation would produce a different ``k`` that
    still looked well-formed. Not constant time.
    """
    if value < 0:
        raise ValueError(f"int2octets expects a non-negative value, got {value}")
    if value.bit_length() > rlen * 8:
        raise ValueError(f"value does not fit in {rlen} octets; the caller must reduce mod q first")
    return value.to_bytes(rlen, "big")


def bits2octets(data: bytes, q: int) -> bytes:
    """
    RFC 6979 section 2.3.4: reduce ``data`` modulo ``q`` and encode it.

    This is the message-hash transform the HMAC seed uses. It runs
    :func:`bits2int` first, then subtracts ``q`` at most once -- the RFC's
    ``z2 = z1 mod q`` where ``z1 < 2*q`` because ``z1`` has at most ``qlen``
    bits. Not constant time.
    """
    qlen, rlen = _qlen_rlen(q)
    z1 = bits2int(data, qlen)
    z2 = z1 - q if z1 >= q else z1
    return int2octets(z2, rlen)


def generate_k(
    q: int,
    private_key: int,
    message_hash: bytes,
    *,
    hash_factory: HashFactory = hashlib.sha256,
) -> int:
    """
    The RFC 6979 deterministic nonce ``k`` in ``[1, q)``.

    ``q`` is the group order, ``private_key`` a scalar in ``[1, q)``, and
    ``message_hash`` the already-computed digest of the message as octets --
    hashing is the caller's job, and ``hash_factory`` is the hash used *inside*
    the DRBG, which need not be the one that produced ``message_hash`` but is
    by convention.

    The candidate loop rejects ``k == 0`` and ``k >= q`` and reseeds, exactly
    as the RFC requires. That loop is why the output is uniform in ``[1, q)``
    with no bias: skipping it and reducing modulo ``q`` instead would
    reintroduce the modulo bias the whole exercise exists to remove.

    Preconditions are checked, because a wrong ``q`` or an out-of-range key
    silently produces a ``k`` that is not the RFC's and would not match any
    verifier's expectation. Not constant time; it derives a secret -- see the
    module docstring.
    """
    if q < 3:
        raise ValueError(f"q must be a group order >= 3, got {q}")
    if not 1 <= private_key < q:
        raise ValueError("private_key must lie in [1, q)")

    qlen, rlen = _qlen_rlen(q)
    hash_size = hash_factory().digest_size

    # RFC 6979 section 3.2 steps a-d.
    x_octets = int2octets(private_key, rlen)
    h1_octets = bits2octets(message_hash, q)
    seed = x_octets + h1_octets

    v = b"\x01" * hash_size
    key = b"\x00" * hash_size

    def _hmac(k: bytes, *chunks: bytes) -> bytes:
        mac = hmac.new(k, digestmod=hash_factory)
        for chunk in chunks:
            mac.update(chunk)
        return mac.digest()

    # Steps d, e, f, g.
    key = _hmac(key, v, b"\x00", seed)
    v = _hmac(key, v)
    key = _hmac(key, v, b"\x01", seed)
    v = _hmac(key, v)

    # Step h: generate candidates until one lands in [1, q).
    while True:
        t = b""
        while len(t) < rlen:
            v = _hmac(key, v)
            t += v
        candidate = bits2int(t, qlen)
        if 1 <= candidate < q:
            return candidate
        key = _hmac(key, v, b"\x00")
        v = _hmac(key, v)
