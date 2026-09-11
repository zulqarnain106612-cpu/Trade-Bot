"""
secp256k1: the curve every Bitcoin and Ethereum signature lives on.

Owns two registry entries:

* ``elliptic-curves`` -- the group law and point encoding.
* ``cyclic-groups-dlp`` -- the fact that this group is cyclic of prime order,
  which is what makes the discrete logarithm the hard problem it needs to be.

The parameters are SEC 2 v2's, and are asserted against the standard at import
rather than trusted: a curve constant that is wrong by one bit is still a
perfectly good group, just not this one, and every signature verified against
it would be meaningless in a way nothing downstream could detect.

Two properties of secp256k1 shape the API:

* **The cofactor is 1.** Every point on the curve except infinity is in the
  prime-order subgroup, so an on-curve check *is* a subgroup check here. That
  is not true of curves with a cofactor, so :func:`validate_public_key` says
  what it is relying on rather than leaving the next curve's reader to assume
  the same holds there.
* **The order ``n`` is prime.** So every non-identity point generates the whole
  group, and the scalars form a field -- which is why ``s`` can be inverted in
  ECDSA verification at all.

**Constant time: none of it.** :func:`scalar_multiply` is double-and-add with a
data-dependent branch per bit; CPython integers branch on width. Signature
*verification* uses only public values, which is what this module is for.
**Never put a private key through it.**

References: SEC 2 v2 sec. 2.4.1; SEC 1 v2 sec. 2.3.3-2.3.4 and 4.1.4;
BIP-62 and BIP-146 (low-s); RFC 6979.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fields.prime_field import PrimeField, Reduction
from ..numbertheory.residues import sqrt_mod_prime

__all__ = [
    "A",
    "B",
    "CURVE_ORDER",
    "FIELD_PRIME",
    "GENERATOR",
    "INFINITY",
    "Point",
    "add",
    "decompress",
    "double",
    "is_on_curve",
    "negate",
    "parse_point",
    "scalar_multiply",
    "serialize_point",
    "validate_public_key",
    "verify_ecdsa",
]


# SEC 2 v2, section 2.4.1.
FIELD_PRIME = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
A = 0
B = 7
_GENERATOR_X = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GENERATOR_Y = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
COFACTOR = 1

# Half the order, rounded down. A signature with ``s`` above this is the
# malleable twin of one below it; see :func:`verify_ecdsa`.
_HALF_ORDER = CURVE_ORDER // 2

_FIELD = PrimeField(FIELD_PRIME, Reduction.PSEUDO_MERSENNE)
_SCALARS = PrimeField(CURVE_ORDER)


@dataclass(frozen=True, slots=True)
class Point:
    """
    An affine point, or the point at infinity when both coordinates are ``None``.

    Frozen because a mutable point is a footgun: curve code passes points
    around freely, and a caller that adjusts one in place corrupts every other
    reference to it. Construct a new one instead.

    Constructing a ``Point`` does **not** check that it is on the curve --
    :func:`decompress` and :func:`parse_point` produce checked points, and
    :func:`is_on_curve` answers the question directly. Making the constructor
    validate would make it impossible to name an off-curve point in a test,
    which is exactly what the boundary tests need to do.
    """

    x: int | None
    y: int | None

    def __post_init__(self) -> None:
        if (self.x is None) != (self.y is None):
            raise ValueError(
                "a Point has both coordinates or neither; one None coordinate is "
                "not the point at infinity, it is a bug"
            )

    @property
    def is_infinity(self) -> bool:
        """Whether this is the group identity. Not constant time."""
        return self.x is None

    def __repr__(self) -> str:
        if self.is_infinity:
            return "Point(infinity)"
        return f"Point(x=0x{self.x:064x}, y=0x{self.y:064x})"


INFINITY = Point(None, None)
GENERATOR = Point(_GENERATOR_X, _GENERATOR_Y)


def is_on_curve(point: Point) -> bool:
    """
    Whether ``point`` satisfies ``y**2 == x**3 + 7`` over the field.

    The point at infinity is on the curve, being the identity of the group the
    curve defines. Coordinates outside ``[0, p)`` are **not** reduced first:
    an out-of-range coordinate is a malformed point, not an unreduced one, and
    accepting it here would let two encodings of one point through the boundary
    checks that call this. Not constant time.
    """
    if point.is_infinity:
        return True
    x, y = point.x, point.y
    if not (0 <= x < FIELD_PRIME and 0 <= y < FIELD_PRIME):
        return False
    return (y * y - x * x * x - B) % FIELD_PRIME == 0


def negate(point: Point) -> Point:
    """The inverse of ``point`` in the group, ``(x, -y)``. Not constant time."""
    if point.is_infinity:
        return INFINITY
    return Point(point.x, (-point.y) % FIELD_PRIME)


def double(point: Point) -> Point:
    """
    ``point + point``.

    A point with ``y == 0`` doubles to infinity -- it is its own inverse. That
    cannot happen on secp256k1, whose order is odd and so has no 2-torsion, but
    the branch is here because the alternative is a division by zero that only
    a curve change would ever reach. Not constant time.
    """
    if point.is_infinity or point.y == 0:
        return INFINITY
    x, y = point.x, point.y
    # lambda = (3x^2 + a) / 2y, with a = 0.
    slope = _FIELD.div(3 * x * x, 2 * y)
    xr = _FIELD.sub(slope * slope, 2 * x)
    return Point(xr, _FIELD.sub(slope * (x - xr), y))


def add(p: Point, q: Point) -> Point:
    """
    The group law: ``p + q``.

    Handles all four special cases explicitly -- either operand at infinity,
    the two being inverses, and the two being equal -- because the chord
    formula divides by ``x_q - x_p`` and every one of these makes that zero.
    Not constant time.
    """
    if p.is_infinity:
        return q
    if q.is_infinity:
        return p
    if p.x == q.x:
        return double(p) if p.y == q.y else INFINITY
    slope = _FIELD.div(q.y - p.y, q.x - p.x)
    xr = _FIELD.sub(slope * slope, p.x + q.x)
    return Point(xr, _FIELD.sub(slope * (p.x - xr), p.y))


def scalar_multiply(scalar: int, point: Point = GENERATOR) -> Point:
    """
    ``scalar * point``, by double-and-add from the most significant bit.

    The scalar is reduced modulo the group order first, and a negative scalar
    negates the point -- both are exact, since the group has order
    :data:`CURVE_ORDER`.

    **Not constant time, and the branch pattern is the scalar.** This is a
    key-recovery oracle if the scalar is secret. It is here for verification
    and analysis, where every input is already public.
    """
    if point.is_infinity:
        return INFINITY
    scalar %= CURVE_ORDER
    if scalar == 0:
        return INFINITY

    result = INFINITY
    for bit in bin(scalar)[2:]:
        result = double(result)
        if bit == "1":
            result = add(result, point)
    return result


def decompress(x: int, y_is_odd: bool) -> Point | None:
    """
    Recover the point with this ``x`` and this ``y`` parity, or ``None``.

    ``None`` means no such point exists: ``x**3 + 7`` is a non-residue, so that
    ``x`` is simply not on the curve. Roughly half of all field elements are
    not, so this is an ordinary outcome for attacker-supplied data and must not
    be treated as an error condition -- but it must also never be treated as a
    point.

    Returns ``None`` rather than raising for an out-of-range ``x`` too, for the
    same reason: a 32-byte string that does not encode a point is not
    exceptional, it is just not a point. Not constant time.
    """
    if not 0 <= x < FIELD_PRIME:
        return None
    y = sqrt_mod_prime((x * x * x + B) % FIELD_PRIME, FIELD_PRIME)
    if y is None:
        return None
    if (y & 1) != y_is_odd:
        y = FIELD_PRIME - y
    return Point(x, y)


def parse_point(data: bytes) -> Point | None:
    """
    Parse a SEC 1 encoded point, returning ``None`` for anything malformed.

    Accepts the 33-byte compressed forms (``0x02``/``0x03`` for even/odd ``y``)
    and the 65-byte uncompressed form (``0x04``). Rejects everything else,
    including the 0x00 infinity encoding: an all-zero point is never a valid
    public key, and accepting it here would put the identity into signature
    verification, where it verifies every signature.

    An uncompressed point whose coordinates do not satisfy the curve equation
    is rejected here rather than deeper in. That is the whole purpose of this
    function existing separately from :class:`Point`. Not constant time.
    """
    if len(data) == 33 and data[0] in (2, 3):
        return decompress(int.from_bytes(data[1:], "big"), data[0] == 3)
    if len(data) == 65 and data[0] == 4:
        point = Point(int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
        return point if is_on_curve(point) else None
    return None


def serialize_point(point: Point, *, compressed: bool = True) -> bytes:
    """
    Encode a point in SEC 1 form.

    Refuses the point at infinity: it has no SEC 1 encoding that is meaningful
    as a public key, and inventing one would produce bytes that
    :func:`parse_point` rejects -- a round trip that silently is not one.
    Not constant time.
    """
    if point.is_infinity:
        raise ValueError("the point at infinity has no public-key encoding")
    x = point.x.to_bytes(32, "big")
    if compressed:
        return bytes([2 + (point.y & 1)]) + x
    return b"\x04" + x + point.y.to_bytes(32, "big")


def validate_public_key(point: Point) -> bool:
    """
    Whether ``point`` is a usable secp256k1 public key.

    On the curve, and not the identity. There is deliberately no separate
    subgroup check: secp256k1's cofactor is 1, so the curve group *is* the
    prime-order group and an on-curve point cannot be in a small subgroup.
    A curve with a cofactor needs the extra check, and copying this function
    to one without adding it is the mistake this docstring exists to prevent.
    Not constant time.
    """
    return not point.is_infinity and is_on_curve(point)


def verify_ecdsa(
    message_hash: int,
    signature: tuple[int, int],
    public_key: Point,
    *,
    require_low_s: bool = True,
) -> bool:
    """
    Verify an ECDSA signature over ``message_hash``.

    ``message_hash`` is the integer already derived from the message digest --
    hashing is the caller's job, and doing it here would hide which digest was
    used.

    ``require_low_s`` defaults to **True**. For every valid ``(r, s)`` the pair
    ``(r, n - s)`` is equally valid, so a signature has two encodings and its
    hash is not a stable identifier. Bitcoin's consensus rules and BIP-146 make
    the low form the only acceptable one. The flag exists because historical
    chain data contains high-``s`` signatures that were valid when mined, and
    an analysis tool that cannot read them is useless -- but the default is the
    strict one, so reading old data is a decision someone made on purpose.

    Returns ``False`` for a malformed signature or an invalid key rather than
    raising: a bad signature is the ordinary case here, not an exception.
    Not constant time -- every input is public.
    """
    r, s = signature
    if not (1 <= r < CURVE_ORDER and 1 <= s < CURVE_ORDER):
        return False
    if require_low_s and s > _HALF_ORDER:
        return False
    if not validate_public_key(public_key):
        return False

    s_inverse = _SCALARS.inv(s)
    u1 = _SCALARS.mul(message_hash, s_inverse)
    u2 = _SCALARS.mul(r, s_inverse)
    point = add(scalar_multiply(u1, GENERATOR), scalar_multiply(u2, public_key))
    if point.is_infinity:
        return False
    return point.x % CURVE_ORDER == r


def _assert_standard_parameters(
    generator: Point = GENERATOR,
    order: int = CURVE_ORDER,
    cofactor: int = COFACTOR,
) -> None:
    """
    Check the curve constants against SEC 2 v2 at import.

    A constant wrong by one bit still defines a perfectly good group -- just
    not this one -- and every signature verified against it would be
    meaningless in a way nothing downstream could detect. This runs once and
    costs a few multiplications.

    The parameters are arguments rather than closed-over constants so that a
    test can hand it wrong values and see it complain. A guard nothing ever
    exercises is a guard nobody knows still works.
    """
    if not is_on_curve(generator):
        raise AssertionError("the secp256k1 generator is not on the curve")
    if not scalar_multiply(order, generator).is_infinity:
        raise AssertionError("CURVE_ORDER does not annihilate the generator")
    if cofactor != 1:
        raise AssertionError("secp256k1 has cofactor 1")


_assert_standard_parameters()
