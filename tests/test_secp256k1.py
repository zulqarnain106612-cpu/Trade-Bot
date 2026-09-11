"""
Tests for :mod:`src.mathcore.curves.secp256k1`.

Three kinds of check, in decreasing order of what they prove:

* **Published constants.** The generator and the first few of its multiples
  are compared against the values SEC 2 v2 and the wider ecosystem publish.
  A curve implementation that is wrong anywhere cannot produce these.
* **An independent group law.** A second, deliberately naive double-and-add
  written against raw ``int`` arithmetic -- no ``PrimeField``, no shared code
  with the module -- is compared against :func:`scalar_multiply` across a
  random sweep and every boundary scalar.
* **Boundary rejection.** Off-curve points, malformed encodings, the identity
  and high-``s`` signatures are asserted to be refused at the parsing and
  verification boundary rather than deeper in.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.curves.secp256k1 import (
    CURVE_ORDER,
    FIELD_PRIME,
    GENERATOR,
    INFINITY,
    B,
    Point,
    _assert_standard_parameters,
    add,
    decompress,
    double,
    is_on_curve,
    negate,
    parse_point,
    scalar_multiply,
    serialize_point,
    validate_public_key,
    verify_ecdsa,
)

# SEC 2 v2 sec. 2.4.1, and the first two further multiples of G, which appear
# in BIP-340's test vectors and every secp256k1 implementation's fixtures.
G_X = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
G_Y = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
TWO_G_X = 0xC6047F9441ED7D6D3045406E95C07CD85C778E4B8CEF3CA7ABAC09B95C709EE5
TWO_G_Y = 0x1AE168FEA63DC339A3C58419466CEAEEF7F632653266D0E1236431A950CFE52A
THREE_G_X = 0xF9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9


def _naive_add(p: tuple[int, int] | None, q: tuple[int, int] | None):
    """
    The group law on raw tuples, with raw ``int`` modular arithmetic.

    Written against the textbook formulas and sharing no code with the module
    under test -- not its field, not its point type, not its special-case
    handling.
    """
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % FIELD_PRIME == 0:
        return None
    if p == q:
        slope = (3 * p[0] * p[0]) * pow(2 * p[1], -1, FIELD_PRIME) % FIELD_PRIME
    else:
        slope = (q[1] - p[1]) * pow(q[0] - p[0], -1, FIELD_PRIME) % FIELD_PRIME
    x = (slope * slope - p[0] - q[0]) % FIELD_PRIME
    return (x, (slope * (p[0] - x) - p[1]) % FIELD_PRIME)


def _naive_multiply(k: int):
    """Double-and-add over :func:`_naive_add`."""
    k %= CURVE_ORDER
    result = None
    addend = (G_X, G_Y)
    while k:
        if k & 1:
            result = _naive_add(result, addend)
        addend = _naive_add(addend, addend)
        k >>= 1
    return result


def _as_tuple(point: Point):
    return None if point.is_infinity else (point.x, point.y)


def _sign(private_key: int, message_hash: int, nonce: int) -> tuple[int, int]:
    """
    A textbook ECDSA signature, for producing inputs to the verifier.

    Deliberately not part of the module: signing needs a secret nonce and this
    module is documented as unsafe for secrets. It exists here to generate
    verifiable pairs, on a throwaway key, in a test.
    """
    point = scalar_multiply(nonce, GENERATOR)
    r = point.x % CURVE_ORDER
    s = (pow(nonce, -1, CURVE_ORDER) * (message_hash + r * private_key)) % CURVE_ORDER
    return r, s


# ---- published constants ---------------------------------------------------


def test_the_generator_is_the_published_point() -> None:
    assert Point(G_X, G_Y) == GENERATOR
    assert is_on_curve(GENERATOR)


def test_the_first_multiples_match_their_published_values() -> None:
    assert scalar_multiply(2) == Point(TWO_G_X, TWO_G_Y)
    assert scalar_multiply(3).x == THREE_G_X


def test_the_order_annihilates_the_generator() -> None:
    """``n`` is the order of the group, not merely a bound on it."""
    assert scalar_multiply(CURVE_ORDER).is_infinity
    assert scalar_multiply(CURVE_ORDER + 1) == GENERATOR


def test_the_last_multiple_is_the_negated_generator() -> None:
    assert scalar_multiply(CURVE_ORDER - 1) == negate(GENERATOR)


def test_the_field_prime_and_order_are_the_published_constants() -> None:
    assert FIELD_PRIME == 2**256 - 2**32 - 977
    assert CURVE_ORDER == 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    assert B == 7


# ---- the group law against an independent implementation -------------------


def test_scalar_multiplication_matches_a_naive_implementation() -> None:
    rng = random.Random("secp256k1-scalars")
    scalars = [
        0,
        1,
        2,
        3,
        255,
        256,
        CURVE_ORDER - 2,
        CURVE_ORDER - 1,
        CURVE_ORDER,
        CURVE_ORDER + 1,
    ]
    scalars += [rng.randrange(CURVE_ORDER) for _ in range(20)]
    for k in scalars:
        assert _as_tuple(scalar_multiply(k)) == _naive_multiply(k), k


def test_addition_matches_a_naive_implementation() -> None:
    rng = random.Random("secp256k1-adds")
    points = [scalar_multiply(rng.randrange(1, CURVE_ORDER)) for _ in range(10)]
    for p in points:
        for q in points:
            assert _as_tuple(add(p, q)) == _naive_add(_as_tuple(p), _as_tuple(q))


def test_a_negative_scalar_negates_the_point() -> None:
    assert scalar_multiply(-1) == negate(GENERATOR)
    assert scalar_multiply(-5) == negate(scalar_multiply(5))


def test_scalar_multiplication_is_a_homomorphism() -> None:
    rng = random.Random("homomorphism")
    for _ in range(20):
        a = rng.randrange(CURVE_ORDER)
        b = rng.randrange(CURVE_ORDER)
        assert add(scalar_multiply(a), scalar_multiply(b)) == scalar_multiply(a + b)


def test_every_multiple_is_on_the_curve() -> None:
    rng = random.Random("on-curve")
    for _ in range(20):
        assert is_on_curve(scalar_multiply(rng.randrange(CURVE_ORDER)))


# ---- identity and special cases --------------------------------------------


def test_infinity_is_the_identity() -> None:
    assert add(INFINITY, GENERATOR) == GENERATOR
    assert add(GENERATOR, INFINITY) == GENERATOR
    assert add(INFINITY, INFINITY).is_infinity
    assert double(INFINITY).is_infinity
    assert negate(INFINITY).is_infinity
    assert scalar_multiply(5, INFINITY).is_infinity
    assert scalar_multiply(0).is_infinity
    assert is_on_curve(INFINITY)


def test_adding_a_point_to_its_inverse_gives_infinity() -> None:
    assert add(GENERATOR, negate(GENERATOR)).is_infinity


def test_doubling_a_two_torsion_point_gives_infinity() -> None:
    """
    Unreachable on secp256k1 -- its order is odd, so there is no point with
    ``y == 0`` -- but the branch exists so a curve change cannot turn it into
    a division by zero. Exercised through a synthetic point.
    """
    assert double(Point(1, 0)).is_infinity


def test_a_half_constructed_point_is_refused() -> None:
    """One ``None`` coordinate is not infinity, it is a bug."""
    with pytest.raises(ValueError, match="both coordinates or neither"):
        Point(1, None)
    with pytest.raises(ValueError, match="both coordinates or neither"):
        Point(None, 1)


def test_repr_distinguishes_infinity_from_a_point() -> None:
    assert repr(INFINITY) == "Point(infinity)"
    assert repr(GENERATOR).startswith("Point(x=0x79be667e")


# ---- on-curve checking at the boundary -------------------------------------


def test_an_off_curve_point_is_rejected() -> None:
    assert not is_on_curve(Point(GENERATOR.x, GENERATOR.y ^ 1))
    assert not is_on_curve(Point(1, 1))


def test_out_of_range_coordinates_are_rejected_not_reduced() -> None:
    """
    A coordinate at or above ``p`` is a malformed point, not an unreduced one.
    Reducing it would let two byte strings denote one point at the boundary.
    """
    assert not is_on_curve(Point(GENERATOR.x + FIELD_PRIME, GENERATOR.y))
    assert not is_on_curve(Point(GENERATOR.x, GENERATOR.y + FIELD_PRIME))
    assert not is_on_curve(Point(-GENERATOR.x % FIELD_PRIME - FIELD_PRIME, GENERATOR.y))


def test_validate_public_key_rejects_the_identity() -> None:
    """The identity verifies every signature; it is never a public key."""
    assert not validate_public_key(INFINITY)
    assert validate_public_key(GENERATOR)
    assert not validate_public_key(Point(1, 1))


# ---- encoding --------------------------------------------------------------


def test_compressed_round_trip() -> None:
    rng = random.Random("compress")
    for _ in range(20):
        point = scalar_multiply(rng.randrange(1, CURVE_ORDER))
        encoded = serialize_point(point)
        assert len(encoded) == 33
        assert encoded[0] == 2 + (point.y & 1)
        assert parse_point(encoded) == point


def test_uncompressed_round_trip() -> None:
    rng = random.Random("uncompress")
    for _ in range(20):
        point = scalar_multiply(rng.randrange(1, CURVE_ORDER))
        encoded = serialize_point(point, compressed=False)
        assert len(encoded) == 65
        assert encoded[0] == 4
        assert parse_point(encoded) == point


def test_the_generator_has_its_published_compressed_encoding() -> None:
    assert (
        serialize_point(GENERATOR).hex()
        == ("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"[:66])
    )


def test_decompress_recovers_both_parities() -> None:
    even = decompress(GENERATOR.x, y_is_odd=False)
    odd = decompress(GENERATOR.x, y_is_odd=True)
    assert even is not None and odd is not None
    assert even.y % 2 == 0
    assert odd.y % 2 == 1
    assert even.y + odd.y == FIELD_PRIME
    assert GENERATOR in (even, odd)


def test_decompress_returns_none_for_an_x_that_is_not_on_the_curve() -> None:
    """
    About half of all field elements have no point. That is ordinary input,
    not an error -- and it must never come back as a point.
    """
    misses = [x for x in range(1, 40) if decompress(x, y_is_odd=False) is None]
    assert misses, "the sweep found no non-residue; the test proved nothing"
    for x in misses:
        assert decompress(x, y_is_odd=True) is None


def test_decompress_rejects_an_out_of_range_x() -> None:
    assert decompress(FIELD_PRIME, y_is_odd=False) is None
    assert decompress(-1, y_is_odd=False) is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x02",
        b"\x00" * 33,
        b"\x04" * 33,
        b"\x02" + b"\x00" * 31,
        b"\x02" + b"\x00" * 33,
        b"\x05" + b"\x00" * 64,
        b"\x04" + b"\x00" * 64,
    ],
)
def test_malformed_encodings_are_rejected(data: bytes) -> None:
    assert parse_point(data) is None


def test_an_off_curve_uncompressed_point_is_rejected_at_the_boundary() -> None:
    """
    The whole point of ``parse_point`` existing: an attacker-supplied 65-byte
    string with arbitrary coordinates must not become a ``Point`` that later
    code trusts.
    """
    forged = b"\x04" + (GENERATOR.x).to_bytes(32, "big") + (1).to_bytes(32, "big")
    assert parse_point(forged) is None


def test_serialising_infinity_is_refused() -> None:
    with pytest.raises(ValueError, match="point at infinity"):
        serialize_point(INFINITY)


# ---- ECDSA verification ----------------------------------------------------


def test_a_valid_signature_verifies() -> None:
    rng = random.Random("ecdsa")
    private_key = rng.randrange(1, CURVE_ORDER)
    public_key = scalar_multiply(private_key)
    for _ in range(10):
        message_hash = rng.randrange(CURVE_ORDER)
        nonce = rng.randrange(1, CURVE_ORDER)
        r, s = _sign(private_key, message_hash, nonce)
        low_s = min(s, CURVE_ORDER - s)
        assert verify_ecdsa(message_hash, (r, low_s), public_key)


def test_a_signature_over_a_different_message_fails() -> None:
    rng = random.Random("wrong-message")
    private_key = rng.randrange(1, CURVE_ORDER)
    public_key = scalar_multiply(private_key)
    r, s = _sign(private_key, 12345, rng.randrange(1, CURVE_ORDER))
    low_s = min(s, CURVE_ORDER - s)
    assert not verify_ecdsa(12346, (r, low_s), public_key)


def test_a_signature_from_a_different_key_fails() -> None:
    rng = random.Random("wrong-key")
    r, s = _sign(rng.randrange(1, CURVE_ORDER), 999, rng.randrange(1, CURVE_ORDER))
    other_key = scalar_multiply(rng.randrange(1, CURVE_ORDER))
    assert not verify_ecdsa(999, (r, min(s, CURVE_ORDER - s)), other_key)


def test_high_s_is_rejected_by_default_and_accepted_on_request() -> None:
    """
    ``(r, n - s)`` is as valid as ``(r, s)``, so a signature has two encodings
    and its hash is not an identifier. BIP-146 makes the low form the only
    acceptable one -- but historical chain data contains the high form, and an
    analysis tool that cannot read it is useless.
    """
    rng = random.Random("malleability")
    private_key = rng.randrange(1, CURVE_ORDER)
    public_key = scalar_multiply(private_key)
    r, s = _sign(private_key, 4242, rng.randrange(1, CURVE_ORDER))
    low_s = min(s, CURVE_ORDER - s)
    high_s = CURVE_ORDER - low_s

    assert verify_ecdsa(4242, (r, low_s), public_key)
    assert not verify_ecdsa(4242, (r, high_s), public_key)
    assert verify_ecdsa(4242, (r, high_s), public_key, require_low_s=False)
    assert verify_ecdsa(4242, (r, low_s), public_key, require_low_s=False)


@pytest.mark.parametrize(
    "signature",
    [
        (0, 1),
        (1, 0),
        (CURVE_ORDER, 1),
        (1, CURVE_ORDER),
        (-1, 1),
        (1, -1),
    ],
)
def test_out_of_range_signature_components_are_rejected(
    signature: tuple[int, int],
) -> None:
    assert not verify_ecdsa(1, signature, GENERATOR, require_low_s=False)


def test_a_signature_against_an_invalid_public_key_fails() -> None:
    assert not verify_ecdsa(1, (1, 1), INFINITY, require_low_s=False)
    assert not verify_ecdsa(1, (1, 1), Point(1, 1), require_low_s=False)


def test_verification_returns_false_rather_than_raising_on_a_bad_signature() -> None:
    """A bad signature is the ordinary case; it must not be an exception."""
    assert verify_ecdsa(1, (2, 3), GENERATOR) is False


# ---- the import-time parameter check ---------------------------------------


def test_the_parameter_check_passes_on_the_real_constants() -> None:
    _assert_standard_parameters()


def test_the_parameter_check_catches_an_off_curve_generator() -> None:
    with pytest.raises(AssertionError, match="not on the curve"):
        _assert_standard_parameters(generator=Point(1, 1))


def test_the_parameter_check_catches_a_wrong_order() -> None:
    """One bit off in ``n`` and the group is not the one the standard names."""
    with pytest.raises(AssertionError, match="annihilate"):
        _assert_standard_parameters(order=CURVE_ORDER ^ 1)


def test_the_parameter_check_catches_a_wrong_cofactor() -> None:
    with pytest.raises(AssertionError, match="cofactor"):
        _assert_standard_parameters(cofactor=8)


def test_a_signature_whose_verification_lands_on_infinity_fails() -> None:
    """
    ``u1*G + u2*Q`` can be the identity when ``h + r*d == 0 (mod n)``. The
    identity has no x coordinate to compare against ``r``, so the check must
    reject rather than reach for one.
    """
    private_key = 0x1234567890ABCDEF
    public_key = scalar_multiply(private_key)
    r, s = 12345, 1
    message_hash = (-r * private_key) % CURVE_ORDER
    assert not verify_ecdsa(message_hash, (r, s), public_key)
