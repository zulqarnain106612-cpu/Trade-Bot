"""
Ed25519: the twisted Edwards curve behind RFC 8032 signatures.

Owns the ``edwards-curves`` registry entry.

Two things distinguish this curve from secp256k1, and both shape the API.

**The addition law is complete.** For a twisted Edwards curve with ``a`` a
square and ``d`` a non-square -- which is exactly the case here -- the addition
formula is correct for *every* pair of points, including doubling, the
identity, and a point plus its inverse. There are no special cases and
therefore no special-case branches to get wrong or to leak through. This is
the single biggest reason Ed25519 exists.

**The cofactor is 8.** The curve group is eight times larger than the
prime-order subgroup signatures live in, so an on-curve point is *not*
necessarily a valid public key, and two different verification equations are
in circulation:

* **cofactorless**, ``[S]B == R + [k]A`` -- what RFC 8032 section 5.1.7 writes
  and what almost every library implements.
* **cofactored**, ``[8S]B == [8]R + [8k]A`` -- what the same RFC permits, what
  ZIP-215 mandates, and what makes verification agree across implementations
  for the small set of signatures where the two disagree.

Neither is "the" answer, and a library that silently picks one is the reason
consensus systems have forked over signature validity. :func:`verify` takes
the choice as a required keyword: a caller must say which rule they are
verifying under, because for some inputs the answer genuinely differs and
the difference is a chain split.

**Constant time: none of it.** Signature verification touches only public
values, which is what this module is for. **Never put a private key or a nonce
through it.**

References: RFC 8032 sec. 5.1 and 7.1; Bernstein et al., *High-speed
high-security signatures* (2011); ZIP-215; Chalkias, Garillot & Nikolaenko,
*Taming the many EdDSAs* (2020).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..numbertheory.residues import sqrt_mod_prime

__all__ = [
    "BASE_POINT",
    "COFACTOR",
    "D",
    "FIELD_PRIME",
    "GROUP_ORDER",
    "IDENTITY",
    "Point",
    "add",
    "decode_point",
    "encode_point",
    "is_on_curve",
    "is_small_order",
    "negate",
    "scalar_multiply",
    "verify",
]


FIELD_PRIME = (1 << 255) - 19
# d = -121665 / 121666 mod p, RFC 8032 sec. 5.1.
D = (-121665 * pow(121666, -1, FIELD_PRIME)) % FIELD_PRIME
# The prime order of the subgroup signatures live in, and the cofactor.
GROUP_ORDER = (1 << 252) + 27742317777372353535851937790883648493
COFACTOR = 8


@dataclass(frozen=True, slots=True)
class Point:
    """
    An affine point on ``-x**2 + y**2 == 1 + d*x**2*y**2``.

    Affine rather than extended coordinates: this module is for checking and
    analysis, where being obviously the equation on the page matters more than
    saving a field inversion. The identity is ``(0, 1)`` and is an ordinary
    point, which is the completeness property doing its work -- there is no
    separate infinity case to represent or to forget.

    Frozen, for the same reason as the secp256k1 point: shared mutable curve
    points corrupt every reference at once.
    """

    x: int
    y: int


IDENTITY = Point(0, 1)


def is_on_curve(point: Point) -> bool:
    """
    Whether ``point`` satisfies the curve equation over the field.

    Coordinates outside ``[0, p)`` are rejected rather than reduced: an
    out-of-range coordinate is a malformed point, and reducing it would let two
    encodings denote one point. Not constant time.
    """
    x, y = point.x, point.y
    if not (0 <= x < FIELD_PRIME and 0 <= y < FIELD_PRIME):
        return False
    left = (-x * x + y * y) % FIELD_PRIME
    right = (1 + D * x * x % FIELD_PRIME * y % FIELD_PRIME * y) % FIELD_PRIME
    return left == right


def negate(point: Point) -> Point:
    """The inverse of ``point``, ``(-x, y)``. Not constant time."""
    return Point((-point.x) % FIELD_PRIME, point.y)


def add(p: Point, q: Point) -> Point:
    """
    The twisted Edwards addition law, with no special cases.

    ``x3 = (x1*y2 + x2*y1) / (1 + d*x1*x2*y1*y2)``,
    ``y3 = (y1*y2 + x1*x2) / (1 - d*x1*x2*y1*y2)``.

    Both denominators are non-zero for every pair of curve points, because
    ``d`` is a non-square modulo ``p``. That is a theorem about this curve, not
    a property of the inputs, so this function has no branches at all -- and
    doubling, adding the identity, and adding an inverse all go down the same
    path. Not constant time (the field inversion is not), but branch-free.
    """
    x1, y1, x2, y2 = p.x, p.y, q.x, q.y
    common = D * x1 * x2 % FIELD_PRIME * y1 % FIELD_PRIME * y2 % FIELD_PRIME
    x3 = (x1 * y2 + x2 * y1) * pow(1 + common, -1, FIELD_PRIME) % FIELD_PRIME
    y3 = (y1 * y2 + x1 * x2) * pow(1 - common, -1, FIELD_PRIME) % FIELD_PRIME
    return Point(x3, y3)


def scalar_multiply(scalar: int, point: Point) -> Point:
    """
    ``scalar * point``, by double-and-add from the most significant bit.

    The scalar is **not** reduced modulo :data:`GROUP_ORDER`. On a curve with a
    cofactor that reduction is wrong for a point outside the prime-order
    subgroup, where the order is ``8*L`` rather than ``L``, and quietly doing
    it would make the cofactored verification equation collapse into the
    cofactorless one. A negative scalar negates the point.

    **Not constant time, and the branch pattern is the scalar.**
    """
    if scalar < 0:
        return scalar_multiply(-scalar, negate(point))
    result = IDENTITY
    if scalar == 0:
        return result
    for bit in bin(scalar)[2:]:
        result = add(result, result)
        if bit == "1":
            result = add(result, point)
    return result


def _recover_x(y: int, x_is_odd: bool) -> int | None:
    """
    The ``x`` matching ``y`` and this sign bit, or ``None`` if none exists.

    ``x**2 = (y**2 - 1) / (d*y**2 + 1)``. A ``y`` with a non-square right-hand
    side is not on the curve; a zero ``x`` combined with a set sign bit is the
    non-canonical encoding RFC 8032 rejects, and it is rejected here.

    The denominator is never zero, and that is a theorem rather than an
    assumption: it vanishes only when ``y**2 == -1/d``, and ``-1/d`` is a
    non-square modulo ``p`` because ``d`` is a non-square and ``-1`` is a
    square. So there is no guard for it -- an unreachable branch would read as
    a handled case and would never be exercised.

    Not constant time.
    """
    numerator = (y * y - 1) % FIELD_PRIME
    denominator = (D * y % FIELD_PRIME * y + 1) % FIELD_PRIME
    x = sqrt_mod_prime(numerator * pow(denominator, -1, FIELD_PRIME), FIELD_PRIME)
    if x is None:
        return None
    if x == 0 and x_is_odd:
        return None
    if (x & 1) != x_is_odd:
        x = FIELD_PRIME - x
    return x


def decode_point(data: bytes) -> Point | None:
    """
    Decode a 32-byte RFC 8032 point, or return ``None``.

    The encoding is ``y`` little-endian in the low 255 bits with the sign of
    ``x`` in the top bit. ``None`` covers every malformed case: wrong length, a
    ``y`` at or above ``p`` (a non-canonical encoding of a valid point, which
    must not be accepted as a second spelling), a ``y`` with no matching ``x``,
    and the zero-``x``-with-sign-set encoding.

    Returns ``None`` rather than raising: a public key or an ``R`` value that
    does not decode is the ordinary case for attacker-supplied data.
    Not constant time.
    """
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    x_is_odd = bool(value >> 255)
    y = value & ((1 << 255) - 1)
    if y >= FIELD_PRIME:
        return None
    x = _recover_x(y, x_is_odd)
    if x is None:
        return None
    return Point(x, y)


def encode_point(point: Point) -> bytes:
    """
    Encode a point in RFC 8032's 32-byte form.

    Refuses a point whose coordinates are out of range, since the encoding
    would silently truncate and produce bytes denoting a different point.
    Not constant time.
    """
    if not (0 <= point.x < FIELD_PRIME and 0 <= point.y < FIELD_PRIME):
        raise ValueError("cannot encode a point with out-of-range coordinates")
    return (point.y | ((point.x & 1) << 255)).to_bytes(32, "little")


def _base_point() -> Point:
    """The RFC 8032 base point: ``y = 4/5``, with ``x`` even."""
    y = 4 * pow(5, -1, FIELD_PRIME) % FIELD_PRIME
    x = _recover_x(y, x_is_odd=False)
    if x is None:  # pragma: no cover - fixed constants; a miss means a typo
        raise AssertionError("the Ed25519 base point y has no matching x")
    return Point(x, y)


BASE_POINT = _base_point()


def is_small_order(point: Point) -> bool:
    """
    Whether ``point`` lies in the order-8 torsion subgroup.

    These eight points are the ones the cofactor introduces. They are on the
    curve and encode perfectly well, but as a public key one of them makes some
    signature verify for anyone. Callers that accept keys from strangers should
    reject them; RFC 8032 does not require it, which is why this is offered
    rather than enforced. Not constant time.
    """
    return scalar_multiply(COFACTOR, point) == IDENTITY


def verify(
    public_key: bytes,
    message: bytes,
    signature: bytes,
    *,
    cofactored: bool,
) -> bool:
    """
    Verify an Ed25519 signature under an explicitly chosen equation.

    ``cofactored`` is a **required keyword** with no default. RFC 8032 writes
    the cofactorless equation but permits the cofactored one, ZIP-215 mandates
    cofactored, and for a small set of signatures the two genuinely disagree.
    Consensus systems have forked over exactly that. A default here would be a
    library quietly picking a side of a disagreement on the caller's behalf,
    so there is none.

    Returns ``False`` for every malformed input -- wrong lengths, an ``R`` or
    ``A`` that does not decode, an ``S`` at or above the group order. A
    non-canonical ``S`` is malleable: ``S + L`` would verify under the same
    arithmetic, giving one signature two encodings, so the range check is part
    of the verification rather than a nicety.

    Not constant time; every input is public.
    """
    if len(signature) != 64 or len(public_key) != 32:
        return False

    r_point = decode_point(signature[:32])
    a_point = decode_point(public_key)
    if r_point is None or a_point is None:
        return False

    s = int.from_bytes(signature[32:], "little")
    if s >= GROUP_ORDER:
        return False

    digest = hashlib.sha512(signature[:32] + public_key + message).digest()
    k = int.from_bytes(digest, "little") % GROUP_ORDER

    left = scalar_multiply(s, BASE_POINT)
    right = add(r_point, scalar_multiply(k, a_point))
    if cofactored:
        left = scalar_multiply(COFACTOR, left)
        right = scalar_multiply(COFACTOR, right)
    return left == right
