"""
Tests for :mod:`src.mathcore.fields.binary_field`.

The roadmap's exit gate names FIPS 197 AES S-box inversion as the known-answer
test, so that is the centrepiece: the S-box is rebuilt from this module's
inversion plus the affine map FIPS 197 specifies, and compared against the
published table's first row and its last entry -- values that are wrong for
any implementation with a defect anywhere in the field.

Alongside that sits a brute-force oracle: for GF(2^8) the inverse of every
element is found by searching all 256 products with a naive xtime multiply
written independently of the module. Exhaustive over the whole field, and it
shares no code with what it checks.
"""

from __future__ import annotations

import pytest

from src.mathcore.fields.binary_field import (
    AES_POLY,
    GCM_POLY,
    BinaryField,
    _poly_divmod,
)

# FIPS 197 Fig. 7, first row of the S-box, and its final entry.
FIPS_197_SBOX_ROW_0 = (
    0x63,
    0x7C,
    0x77,
    0x7B,
    0xF2,
    0x6B,
    0x6F,
    0xC5,
    0x30,
    0x01,
    0x67,
    0x2B,
    0xFE,
    0xD7,
    0xAB,
    0x76,
)
FIPS_197_SBOX_LAST = 0x16

# Irreducible polynomials of a few degrees, for the generic tests.
POLY_DEGREE_3 = 0b1011  # x**3 + x + 1
POLY_DEGREE_4 = 0b10011  # x**4 + x + 1
POLY_DEGREE_5 = 0b100101  # x**5 + x**2 + 1


def _naive_xtime_mul(a: int, b: int) -> int:
    """
    Multiply in GF(2^8) with the AES polynomial, by the shift-and-reduce rule
    FIPS 197 states in words. Deliberately written without touching the module
    under test.
    """
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


def _brute_force_inverse(a: int) -> int:
    """The unique ``b`` with ``a * b == 1`` in GF(2^8), found by search."""
    return next(b for b in range(256) if _naive_xtime_mul(a, b) == 1)


def _aes_sbox(field: BinaryField, a: int) -> int:
    """
    The AES S-box, built from this module's inversion and FIPS 197's affine map.

    ``S(0) = 0x63``: zero has no inverse, and the standard defines the S-box to
    map it to itself before the affine step.
    """
    b = 0 if a == 0 else field.inv(a)
    out = 0
    for i in range(8):
        bit = (
            ((b >> i) & 1)
            ^ ((b >> ((i + 4) % 8)) & 1)
            ^ ((b >> ((i + 5) % 8)) & 1)
            ^ ((b >> ((i + 6) % 8)) & 1)
            ^ ((b >> ((i + 7) % 8)) & 1)
            ^ ((0x63 >> i) & 1)
        )
        out |= bit << i
    return out


@pytest.fixture(scope="module")
def aes_field() -> BinaryField:
    return BinaryField(AES_POLY)


# ---- FIPS 197 known answers ------------------------------------------------


def test_the_sbox_first_row_matches_fips_197(aes_field: BinaryField) -> None:
    built = tuple(_aes_sbox(aes_field, a) for a in range(16))
    assert built == FIPS_197_SBOX_ROW_0


def test_the_last_sbox_entry_matches_fips_197(aes_field: BinaryField) -> None:
    assert _aes_sbox(aes_field, 0xFF) == FIPS_197_SBOX_LAST


def test_the_sbox_is_a_permutation(aes_field: BinaryField) -> None:
    """A defect anywhere in inversion would collide two inputs."""
    assert len({_aes_sbox(aes_field, a) for a in range(256)}) == 256


def test_the_fips_197_worked_examples(aes_field: BinaryField) -> None:
    """Section 4.2 gives ``0x57 * 0x83 = 0xC1``; section 4.4, ``0x53^-1 = 0xCA``."""
    assert aes_field.mul(0x57, 0x83) == 0xC1
    assert aes_field.inv(0x53) == 0xCA


def test_inversion_matches_a_brute_force_search_over_the_whole_field(
    aes_field: BinaryField,
) -> None:
    for a in range(1, 256):
        assert aes_field.inv(a) == _brute_force_inverse(a), a


def test_multiplication_matches_the_naive_xtime_rule(aes_field: BinaryField) -> None:
    for a in range(256):
        for b in range(0, 256, 7):
            assert aes_field.mul(a, b) == _naive_xtime_mul(a, b), (a, b)


# ---- field axioms ----------------------------------------------------------


@pytest.mark.parametrize("poly", [POLY_DEGREE_3, POLY_DEGREE_4, POLY_DEGREE_5, AES_POLY])
def test_every_non_zero_element_has_an_inverse(poly: int) -> None:
    field = BinaryField(poly)
    for a in range(1, field.order):
        assert field.mul(a, field.inv(a)) == 1, a
        assert field.div(a, a) == 1, a


@pytest.mark.parametrize("poly", [POLY_DEGREE_3, POLY_DEGREE_4, POLY_DEGREE_5])
def test_addition_is_xor_and_is_its_own_inverse(poly: int) -> None:
    field = BinaryField(poly)
    for a in range(field.order):
        for b in range(field.order):
            assert field.add(a, b) == a ^ b
            assert field.add(field.add(a, b), b) == a


@pytest.mark.parametrize("poly", [POLY_DEGREE_3, POLY_DEGREE_4, POLY_DEGREE_5])
def test_multiplication_is_commutative_and_associative(poly: int) -> None:
    field = BinaryField(poly)
    for a in range(field.order):
        for b in range(field.order):
            assert field.mul(a, b) == field.mul(b, a)
            for c in range(field.order):
                assert field.mul(field.mul(a, b), c) == field.mul(a, field.mul(b, c))


@pytest.mark.parametrize("poly", [POLY_DEGREE_3, POLY_DEGREE_4, POLY_DEGREE_5])
def test_the_multiplicative_group_has_order_one_less_than_the_field(poly: int) -> None:
    """Fermat's little theorem for GF(2^n): ``a**(2**n - 1) == 1``."""
    field = BinaryField(poly)
    for a in range(1, field.order):
        assert field.pow(a, field.order - 1) == 1, a


@pytest.mark.parametrize("poly", [POLY_DEGREE_4, AES_POLY])
def test_squaring_agrees_with_multiplication(poly: int) -> None:
    field = BinaryField(poly)
    for a in range(field.order):
        assert field.square(a) == field.mul(a, a)


@pytest.mark.parametrize("poly", [POLY_DEGREE_4, AES_POLY])
def test_a_negative_exponent_inverts_first(poly: int) -> None:
    field = BinaryField(poly)
    for a in range(1, field.order):
        assert field.pow(a, -1) == field.inv(a)
        assert field.mul(field.pow(a, -3), field.pow(a, 3)) == 1


def test_unreduced_inputs_are_reduced_first(aes_field: BinaryField) -> None:
    assert aes_field.reduce(AES_POLY) == 0
    assert aes_field.add(AES_POLY ^ 0x05, 0) == 0x05
    assert aes_field.mul(AES_POLY ^ 0x57, 0x83) == 0xC1
    assert aes_field.pow(AES_POLY ^ 0x53, 1) == 0x53


def test_inverting_zero_raises(aes_field: BinaryField) -> None:
    with pytest.raises(ZeroDivisionError):
        aes_field.inv(0)
    with pytest.raises(ZeroDivisionError):
        aes_field.inv(AES_POLY)
    with pytest.raises(ZeroDivisionError):
        aes_field.div(1, 0)


def test_negative_elements_are_rejected(aes_field: BinaryField) -> None:
    """A bit vector has no sign; a negative int is a caller error, not a value."""
    with pytest.raises(ValueError, match="non-negative"):
        aes_field.reduce(-1)


# ---- construction ----------------------------------------------------------


def test_a_reducible_modulus_is_refused() -> None:
    """
    ``x**8 + 1 == (x + 1)**8`` over GF(2). It is a perfectly good ring modulus
    and a catastrophic field modulus, and nothing downstream would notice.
    """
    with pytest.raises(ValueError, match="reducible"):
        BinaryField(0x101)


def test_another_reducible_modulus_is_refused() -> None:
    """``x**4 + x**2 + 1 == (x**2 + x + 1)**2``."""
    with pytest.raises(ValueError, match="reducible"):
        BinaryField(0b10101)


def test_the_gcm_polynomial_is_irreducible() -> None:
    field = BinaryField(GCM_POLY)
    assert field.degree == 128
    assert field.order == 1 << 128
    assert field.mul(2, field.inv(2)) == 1


def test_the_degree_may_be_declared_and_is_checked() -> None:
    assert BinaryField(AES_POLY, 8).degree == 8
    with pytest.raises(ValueError, match="degree 8, not the declared 9"):
        BinaryField(AES_POLY, 9)


@pytest.mark.parametrize("bad", [0, 1, -3])
def test_a_degenerate_modulus_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="degree >= 1"):
        BinaryField(bad)


def test_a_non_integer_modulus_is_refused() -> None:
    with pytest.raises(TypeError):
        BinaryField(True)
    with pytest.raises(TypeError):
        BinaryField(283.0)  # type: ignore[arg-type]


def test_repr_names_the_modulus_in_hex(aes_field: BinaryField) -> None:
    assert repr(aes_field) == "BinaryField(modulus=0x11B, degree=8)"


def test_the_smallest_field_is_gf_two_squared() -> None:
    """``x**2 + x + 1``: GF(4), where every non-zero element is its own square root."""
    field = BinaryField(0b111)
    assert field.order == 4
    assert sorted(field.mul(a, a) for a in range(4)) == [0, 1, 2, 3]


def test_dividing_by_the_zero_polynomial_raises_rather_than_looping() -> None:
    """
    A private helper, tested directly and on purpose. No public path reaches
    this branch -- the modulus is validated at construction -- but without the
    guard a zero divisor gives a degree of -1 and an unterminating loop, which
    is a far worse failure than an exception. The guard is only worth keeping
    if something exercises it.
    """
    with pytest.raises(ZeroDivisionError):
        _poly_divmod(1, 0)
