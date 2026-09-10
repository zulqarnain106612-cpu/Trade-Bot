"""
Tests for :mod:`src.mathcore.curves.ed25519`.

The exit gate the roadmap names is RFC 8032's test vectors including the
negative ones, and an explicit cofactor convention tested in both settings.

The RFC vectors are used twice over: each published signature must verify, and
each published *public key* must be reproducible from its seed by clamping and
scalar multiplication. The second use is the stronger check -- it exercises the
base point, the scalar ladder and the encoding against a value this repository
did not choose.

The cofactor disagreement is not asserted from a fixture but constructed: a
signature is built over a nonce point shifted by a point of order two, which is
the standard way those "Taming the many EdDSAs" vectors are made. Cofactorless
verification must reject it and cofactored must accept it. If both agreed, the
required keyword would be theatre.
"""

from __future__ import annotations

import hashlib

import pytest

from src.mathcore.curves.ed25519 import (
    BASE_POINT,
    COFACTOR,
    FIELD_PRIME,
    GROUP_ORDER,
    IDENTITY,
    D,
    Point,
    add,
    decode_point,
    encode_point,
    is_on_curve,
    is_small_order,
    negate,
    scalar_multiply,
    verify,
)

# RFC 8032 section 7.1, TEST 1, TEST 2 and TEST 3.
RFC_8032_VECTORS = (
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
)

# The RFC 8032 encoding of the base point, and the point of order two.
BASE_POINT_ENCODING = "5866666666666666666666666666666666666666666666666666666666666666"
ORDER_TWO_POINT = Point(0, FIELD_PRIME - 1)


def _secret_scalar(seed: bytes) -> int:
    """RFC 8032's clamping of the first half of ``SHA-512(seed)``."""
    h = int.from_bytes(hashlib.sha512(seed).digest()[:32], "little")
    return (h & ((1 << 254) - 8)) | (1 << 254)


def _sign(seed: bytes, message: bytes, *, nonce_shift: Point | None = None) -> bytes:
    """
    A textbook Ed25519 signature, optionally with the nonce point shifted.

    Signing is not part of the module -- it needs a secret and the module is
    documented as unsafe for secrets -- so it lives here, on throwaway keys,
    to produce inputs for the verifier.

    ``nonce_shift`` adds a point to ``R`` without adjusting ``S``. With a
    small-order shift the result is the construction that separates the two
    verification equations.
    """
    digest = hashlib.sha512(seed).digest()
    a = _secret_scalar(seed)
    public_key = encode_point(scalar_multiply(a, BASE_POINT))

    r = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little")
    r %= GROUP_ORDER
    r_point = scalar_multiply(r, BASE_POINT)
    if nonce_shift is not None:
        r_point = add(r_point, nonce_shift)
    encoded_r = encode_point(r_point)

    k = int.from_bytes(hashlib.sha512(encoded_r + public_key + message).digest(), "little")
    s = (r + (k % GROUP_ORDER) * a) % GROUP_ORDER
    return encoded_r + s.to_bytes(32, "little")


def _public_key(seed: bytes) -> bytes:
    return encode_point(scalar_multiply(_secret_scalar(seed), BASE_POINT))


# ---- RFC 8032 vectors ------------------------------------------------------


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_8032_VECTORS)
def test_the_published_public_key_is_reproduced_from_its_seed(
    seed: str, public: str, message: str, signature: str
) -> None:
    """The stronger half: base point, ladder and encoding against the RFC."""
    assert _public_key(bytes.fromhex(seed)).hex() == public


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_8032_VECTORS)
def test_the_published_signature_verifies_under_both_conventions(
    seed: str, public: str, message: str, signature: str
) -> None:
    """A well-formed signature verifies either way; only edge cases diverge."""
    args = (bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature))
    assert verify(*args, cofactored=False)
    assert verify(*args, cofactored=True)


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_8032_VECTORS)
def test_a_flipped_message_bit_fails(seed: str, public: str, message: str, signature: str) -> None:
    tampered = bytes.fromhex(message) + b"\x00"
    assert not verify(bytes.fromhex(public), tampered, bytes.fromhex(signature), cofactored=False)


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_8032_VECTORS)
def test_a_flipped_signature_bit_fails(
    seed: str, public: str, message: str, signature: str
) -> None:
    raw = bytearray(bytes.fromhex(signature))
    raw[40] ^= 1
    assert not verify(bytes.fromhex(public), bytes.fromhex(message), bytes(raw), cofactored=False)


def test_a_signature_under_the_wrong_key_fails() -> None:
    _seed, public, message, signature = RFC_8032_VECTORS[0]
    other = RFC_8032_VECTORS[1][1]
    assert public != other
    assert not verify(
        bytes.fromhex(other),
        bytes.fromhex(message),
        bytes.fromhex(signature),
        cofactored=False,
    )


# ---- the cofactor convention -----------------------------------------------


def test_the_order_two_point_is_on_the_curve_and_small_order() -> None:
    assert is_on_curve(ORDER_TWO_POINT)
    assert is_small_order(ORDER_TWO_POINT)
    assert add(ORDER_TWO_POINT, ORDER_TWO_POINT) == IDENTITY
    assert not is_small_order(BASE_POINT)
    assert is_small_order(IDENTITY)


def test_the_two_conventions_genuinely_disagree() -> None:
    """
    A signature whose ``R`` is shifted by a point of order two. Cofactorless
    verification sees the shift and rejects; cofactored multiplies it away and
    accepts. If this test ever passes both ways, the required keyword is
    theatre and the module is claiming a distinction it does not make.
    """
    seed = bytes(range(32))
    message = b"cofactor"
    signature = _sign(seed, message, nonce_shift=ORDER_TWO_POINT)
    public_key = _public_key(seed)

    assert not verify(public_key, message, signature, cofactored=False)
    assert verify(public_key, message, signature, cofactored=True)


def test_an_unshifted_signature_from_the_same_signer_verifies_both_ways() -> None:
    """The control for the test above: without the shift there is no divergence."""
    seed = bytes(range(32))
    message = b"cofactor"
    signature = _sign(seed, message)
    public_key = _public_key(seed)

    assert verify(public_key, message, signature, cofactored=False)
    assert verify(public_key, message, signature, cofactored=True)


def test_the_convention_must_be_stated() -> None:
    """No default: a library picking a side of this disagreement is the bug."""
    _seed, public, message, signature = RFC_8032_VECTORS[0]
    with pytest.raises(TypeError):
        verify(  # type: ignore[call-arg]
            bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
        )


# ---- malformed input -------------------------------------------------------


def test_a_non_canonical_s_is_rejected() -> None:
    """
    ``S + L`` verifies under the same arithmetic, so accepting it would give
    one signature two encodings. The range check is part of verification.
    """
    _seed, public, message, signature = RFC_8032_VECTORS[0]
    raw = bytes.fromhex(signature)
    s = int.from_bytes(raw[32:], "little")
    malleable = raw[:32] + (s + GROUP_ORDER).to_bytes(32, "little")

    assert verify(bytes.fromhex(public), bytes.fromhex(message), raw, cofactored=False)
    assert not verify(bytes.fromhex(public), bytes.fromhex(message), malleable, cofactored=False)


@pytest.mark.parametrize("length", [0, 31, 33, 63, 65])
def test_a_signature_of_the_wrong_length_is_rejected(length: int) -> None:
    _seed, public, message, _signature = RFC_8032_VECTORS[0]
    assert not verify(
        bytes.fromhex(public), bytes.fromhex(message), b"\x00" * length, cofactored=False
    )


@pytest.mark.parametrize("length", [0, 31, 33])
def test_a_public_key_of_the_wrong_length_is_rejected(length: int) -> None:
    _seed, _public, message, signature = RFC_8032_VECTORS[0]
    assert not verify(
        b"\x00" * length,
        bytes.fromhex(message),
        bytes.fromhex(signature),
        cofactored=False,
    )


def test_an_undecodable_public_key_is_rejected() -> None:
    _seed, _public, message, signature = RFC_8032_VECTORS[0]
    assert decode_point(b"\x02" * 32) is None
    assert not verify(
        b"\x02" * 32, bytes.fromhex(message), bytes.fromhex(signature), cofactored=False
    )


def test_an_undecodable_r_is_rejected() -> None:
    _seed, public, message, signature = RFC_8032_VECTORS[0]
    forged = b"\x02" * 32 + bytes.fromhex(signature)[32:]
    assert not verify(bytes.fromhex(public), bytes.fromhex(message), forged, cofactored=False)


# ---- encoding --------------------------------------------------------------


def test_the_base_point_has_its_published_encoding() -> None:
    assert encode_point(BASE_POINT).hex() == BASE_POINT_ENCODING
    assert decode_point(bytes.fromhex(BASE_POINT_ENCODING)) == BASE_POINT


def test_encoding_round_trips_over_a_sweep() -> None:
    for k in (1, 2, 3, 7, 255, GROUP_ORDER - 1):
        point = scalar_multiply(k, BASE_POINT)
        assert decode_point(encode_point(point)) == point


def test_a_non_canonical_y_is_rejected() -> None:
    """
    ``y = p`` encodes the same point as ``y = 0`` but as different bytes.
    Accepting it would give one point two spellings at the boundary.
    """
    assert decode_point(FIELD_PRIME.to_bytes(32, "little")) is None
    assert decode_point((FIELD_PRIME + 1).to_bytes(32, "little")) is None


def test_a_y_with_no_matching_x_is_rejected() -> None:
    misses = [y for y in range(2, 60) if decode_point(y.to_bytes(32, "little")) is None]
    assert misses, "the sweep found no non-residue; the test proved nothing"


def test_zero_x_with_the_sign_bit_set_is_rejected() -> None:
    """RFC 8032 sec. 5.1.3 step 3: this encoding is invalid, not a negative zero."""
    encoded = ((1 << 255) | 1).to_bytes(32, "little")
    assert decode_point(encoded) is None
    assert decode_point((1).to_bytes(32, "little")) == IDENTITY


@pytest.mark.parametrize("length", [0, 31, 33])
def test_decoding_the_wrong_length_is_rejected(length: int) -> None:
    assert decode_point(b"\x00" * length) is None


def test_encoding_an_out_of_range_point_is_refused() -> None:
    with pytest.raises(ValueError, match="out-of-range"):
        encode_point(Point(0, FIELD_PRIME))
    with pytest.raises(ValueError, match="out-of-range"):
        encode_point(Point(-1, 1))


# ---- the group law ---------------------------------------------------------


def test_the_identity_is_an_ordinary_point() -> None:
    """Completeness at work: no special case to represent or to forget."""
    assert is_on_curve(IDENTITY)
    assert add(IDENTITY, BASE_POINT) == BASE_POINT
    assert add(BASE_POINT, IDENTITY) == BASE_POINT
    assert add(IDENTITY, IDENTITY) == IDENTITY
    assert scalar_multiply(0, BASE_POINT) == IDENTITY


def test_the_addition_law_needs_no_special_case_for_doubling() -> None:
    point = scalar_multiply(7, BASE_POINT)
    assert add(point, point) == scalar_multiply(14, BASE_POINT)


def test_adding_a_point_to_its_inverse_gives_the_identity() -> None:
    point = scalar_multiply(9, BASE_POINT)
    assert add(point, negate(point)) == IDENTITY
    assert negate(IDENTITY) == IDENTITY


def test_the_group_order_annihilates_the_base_point() -> None:
    assert scalar_multiply(GROUP_ORDER, BASE_POINT) == IDENTITY
    assert scalar_multiply(GROUP_ORDER + 1, BASE_POINT) == BASE_POINT


def test_scalar_multiplication_is_a_homomorphism() -> None:
    for a, b in ((1, 1), (3, 5), (255, 256), (GROUP_ORDER - 1, 2)):
        assert add(
            scalar_multiply(a, BASE_POINT), scalar_multiply(b, BASE_POINT)
        ) == scalar_multiply(a + b, BASE_POINT)


def test_a_negative_scalar_negates_the_point() -> None:
    assert scalar_multiply(-1, BASE_POINT) == negate(BASE_POINT)
    assert scalar_multiply(-5, BASE_POINT) == negate(scalar_multiply(5, BASE_POINT))


def test_the_scalar_is_not_reduced_modulo_the_group_order() -> None:
    """
    On a cofactor curve that reduction is wrong outside the prime-order
    subgroup: the order there is ``8L``, not ``L``. Reducing would collapse the
    cofactored equation into the cofactorless one.
    """
    shifted = add(BASE_POINT, ORDER_TWO_POINT)
    assert scalar_multiply(GROUP_ORDER, shifted) != scalar_multiply(0, shifted)
    assert scalar_multiply(COFACTOR * GROUP_ORDER, shifted) == IDENTITY


def test_off_curve_and_out_of_range_points_are_rejected() -> None:
    assert not is_on_curve(Point(1, 1))
    assert not is_on_curve(Point(BASE_POINT.x + FIELD_PRIME, BASE_POINT.y))
    assert not is_on_curve(Point(BASE_POINT.x, -1))


def test_the_curve_constant_is_the_published_one() -> None:
    assert (-121665 * pow(121666, -1, FIELD_PRIME)) % FIELD_PRIME == D
    assert FIELD_PRIME == 2**255 - 19
    assert GROUP_ORDER == 2**252 + 27742317777372353535851937790883648493
    assert COFACTOR == 8


def test_no_y_makes_the_recovery_denominator_vanish() -> None:
    """
    The theorem the missing guard rests on: ``d*y**2 + 1 == 0`` has no
    solution, because ``-1/d`` is a non-square modulo ``p``. Asserted over the
    field rather than argued, so a future curve constant that broke it would
    fail here instead of raising from inside ``pow``.
    """
    from src.mathcore.numbertheory.residues import sqrt_mod_prime

    target = (-1 * pow(D, -1, FIELD_PRIME)) % FIELD_PRIME
    assert sqrt_mod_prime(target, FIELD_PRIME) is None
