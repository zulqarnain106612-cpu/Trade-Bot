"""
SEC-0001: BLS12-381 refuses a point it was never given the right to trust.

The module once had no point validation of any kind -- not an on-curve check,
not a subgroup check -- while exposing a pairing that an attacker-chosen point
could be fed to. That is the textbook setting for two attacks, and this file
keeps one negative case for each permanently in the suite:

* **Invalid curve.** A point that does not satisfy the curve equation lies on
  some *other* curve, which the attacker picked for having a smooth order. The
  group law does not notice -- it is the same rational formulae -- so a scalar
  multiplication happily runs and its result leaks the scalar modulo the small
  factors of that other curve's order.
* **Small subgroup.** Both G1 and G2 have a cofactor, so a point can satisfy
  the curve equation perfectly and still have small order. The result of a
  scalar multiplication then reveals the scalar modulo that order, and a
  handful of queries with different small orders recover it outright.

The small-subgroup points here are built rather than hard-coded: take any
on-curve point and multiply by ``r``, which annihilates the prime-order part
and leaves exactly the cofactor part. Building them makes the test say why the
point is dangerous instead of asking the reader to trust a constant.

These tests are never deleted. The weakness was possible once, so the check
that it is not possible now is permanent.
"""

from __future__ import annotations

import pytest

from src.mathcore.curves.bls12_381 import (
    B1,
    B2,
    COFACTOR_G1,
    COFACTOR_G2,
    CURVE_ORDER,
    FIELD_MODULUS,
    FQ2,
    FQ12,
    G1,
    G2,
    g1_multiply,
    g1_point,
    g2_multiply,
    g2_point,
    is_in_subgroup_g1,
    is_in_subgroup_g2,
    is_on_curve_g1,
    is_on_curve_g2,
    pairing,
    validate_g1,
    validate_g2,
)
from src.mathcore.curves.bls12_381 import _multiply as _unchecked_multiply

# --- helpers: square roots, used only to find honest on-curve points --------


def _sqrt_fq(a: int) -> int | None:
    """A square root of ``a`` mod p, or None. p = 3 mod 4, so this is one pow."""
    root = pow(a, (FIELD_MODULUS + 1) // 4, FIELD_MODULUS)
    return root if root * root % FIELD_MODULUS == a % FIELD_MODULUS else None


def _sqrt_fq2(a):
    """
    A square root in Fp2 (u^2 = -1) by the complex method, or None.

    Needed because an on-curve G2 point that is *not* in G2 cannot be obtained
    from the generator -- every multiple of the generator is in the subgroup,
    which is the whole point of it. It has to be solved for.
    """
    a0, a1 = a.coeffs
    if a1 == 0:
        root = _sqrt_fq(a0)
        if root is not None:
            return FQ2([root, 0])
        root = _sqrt_fq((-a0) % FIELD_MODULUS)
        return FQ2([0, root]) if root is not None else None
    norm = _sqrt_fq((a0 * a0 + a1 * a1) % FIELD_MODULUS)
    if norm is None:
        return None
    half = pow(2, -1, FIELD_MODULUS)
    for candidate in (norm, (-norm) % FIELD_MODULUS):
        delta = (a0 + candidate) * half % FIELD_MODULUS
        root = _sqrt_fq(delta)
        if root:
            return FQ2([root, a1 * pow(2 * root, -1, FIELD_MODULUS) % FIELD_MODULUS])
    return None


def _some_point_on_e() -> tuple[int, int]:
    """The smallest-x point on E/Fq. Almost certainly not in G1 -- see below."""
    for x in range(1, 100):
        y = _sqrt_fq((x * x * x + B1) % FIELD_MODULUS)
        if y:
            return (x, y)
    raise AssertionError("no point found on E/Fq for small x")


def _some_point_on_twist():
    """The smallest-x point on E'/Fq2. Almost certainly not in G2."""
    for k in range(1, 100):
        x = FQ2([k, 1])
        y = _sqrt_fq2(x * x * x + B2)
        if y is not None:
            return (x, y)
    raise AssertionError("no point found on E'/Fq2 for small x")


def _small_subgroup_g1():
    """An on-curve G1 point of order dividing the cofactor, not 1, not in G1."""
    point = _unchecked_multiply(_some_point_on_e(), CURVE_ORDER)
    assert point is not None, "r killed the point, so it was in G1 after all"
    return point


def _small_subgroup_g2():
    """An on-curve twist point of order dividing the cofactor, not in G2."""
    point = _unchecked_multiply(_some_point_on_twist(), CURVE_ORDER)
    assert point is not None, "r killed the point, so it was in G2 after all"
    return point


# --- the generators are accepted --------------------------------------------


def test_the_generators_pass_every_check() -> None:
    assert is_on_curve_g1(G1) and is_in_subgroup_g1(G1)
    assert is_on_curve_g2(G2) and is_in_subgroup_g2(G2)
    validate_g1(G1)
    validate_g2(G2)


def test_multiples_of_the_generators_stay_valid() -> None:
    """Sanity: the checks accept honest points, not just the generators."""
    for scalar in (2, 7, CURVE_ORDER - 1):
        validate_g1(g1_multiply(scalar))
        validate_g2(g2_multiply(scalar))


def test_the_validated_constructors_return_the_point() -> None:
    assert g1_point(*G1) == G1
    x, y = G2
    assert g2_point(x.coeffs, y.coeffs) == G2


# --- negative case 1: off the curve -----------------------------------------


def test_an_off_curve_g1_point_is_rejected() -> None:
    """y^2 = x^3 + 4 fails, so the point is on some other curve entirely."""
    x, y = G1
    off = (x, (y + 1) % FIELD_MODULUS)
    assert not is_on_curve_g1(off)
    assert not is_in_subgroup_g1(off)
    with pytest.raises(ValueError, match="not on the BLS12-381 curve"):
        validate_g1(off)
    with pytest.raises(ValueError, match="not on the BLS12-381 curve"):
        g1_point(*off)


def test_an_off_curve_g2_point_is_rejected() -> None:
    x, y = G2
    off = (x, y + FQ2([1, 0]))
    assert not is_on_curve_g2(off)
    assert not is_in_subgroup_g2(off)
    with pytest.raises(ValueError, match="not on the BLS12-381 twist"):
        validate_g2(off)
    with pytest.raises(ValueError, match="not on the BLS12-381 twist"):
        g2_point(*off)


def test_the_twist_coefficient_is_not_the_base_curve_coefficient() -> None:
    """
    B2 is 4(1 + u), not 4.

    Checking a G2 point against the base curve's ``b`` would reject every
    honest point; the constant existing but unused is what made the original
    gap visible, so its value is pinned rather than assumed.
    """
    assert B2.coeffs == FQ2([4, 4]).coeffs
    x, y = G2
    assert (y * y - x * x * x - B2).is_zero()
    assert not (y * y - x * x * x - FQ2([B1, 0])).is_zero()


# --- negative case 2: on the curve, in a small subgroup ---------------------


def test_a_small_subgroup_g1_point_is_on_the_curve_but_rejected() -> None:
    point = _small_subgroup_g1()
    assert is_on_curve_g1(point), "the attack point must satisfy the curve equation"
    assert not is_in_subgroup_g1(point)
    with pytest.raises(ValueError, match="prime-order subgroup G1"):
        validate_g1(point)
    with pytest.raises(ValueError, match="prime-order subgroup G1"):
        g1_point(*point)


def test_a_small_subgroup_g2_point_is_on_the_twist_but_rejected() -> None:
    point = _small_subgroup_g2()
    assert is_on_curve_g2(point), "the attack point must satisfy the twist equation"
    assert not is_in_subgroup_g2(point)
    with pytest.raises(ValueError, match="prime-order subgroup G2"):
        validate_g2(point)
    with pytest.raises(ValueError, match="prime-order subgroup G2"):
        g2_point(*point)


def test_the_cofactors_are_the_real_cofactors() -> None:
    """
    ``r * (h * P) == O`` for an on-curve point, while ``r * P != O``.

    Both halves matter: the first says the declared cofactor is the true one,
    the second says these points really are outside the prime-order subgroup,
    so the rejections above are testing what they claim to test.
    """
    g1_off = _some_point_on_e()
    assert _unchecked_multiply(_unchecked_multiply(g1_off, COFACTOR_G1), CURVE_ORDER) is None
    assert _unchecked_multiply(g1_off, CURVE_ORDER) is not None

    g2_off = _some_point_on_twist()
    assert _unchecked_multiply(_unchecked_multiply(g2_off, COFACTOR_G2), CURVE_ORDER) is None
    assert _unchecked_multiply(g2_off, CURVE_ORDER) is not None


# --- malformed coordinates ---------------------------------------------------


def test_out_of_range_coordinates_are_rejected_not_reduced() -> None:
    """
    ``x = p`` is the same residue as ``x = 0``, and must still be refused.

    Reducing here would give one point two encodings, and any check keyed on
    the encoding -- a replay cache, a deduplicated key set -- would see two
    different things.
    """
    x, y = G1
    assert not is_on_curve_g1((x + FIELD_MODULUS, y))
    with pytest.raises(ValueError, match="not on the BLS12-381 curve"):
        g1_point(x + FIELD_MODULUS, y)
    with pytest.raises(ValueError, match="reduced into"):
        g2_point([FIELD_MODULUS, 0], [0, 0])


def test_non_numeric_and_wrong_field_coordinates_are_rejected() -> None:
    """A G1 point is not a G2 point, in either direction, and neither is None-ish junk."""
    assert not is_on_curve_g1(G2)
    assert not is_on_curve_g2(G1)
    with pytest.raises(ValueError, match="two coefficients"):
        g2_point([1], [2])


def test_an_fp12_element_is_not_an_fp2_coordinate() -> None:
    """
    Both coordinate types are ``FQP``, so only the degree tells them apart.

    Without the degree check an Fp12 element would reach the curve equation and
    be compared against an Fp2 constant, which raises somewhere unhelpful at
    best and silently agrees at worst.
    """
    wrong = FQ12([1] + [0] * 11)
    assert not is_on_curve_g2((wrong, wrong))
    with pytest.raises(ValueError, match="two coefficients"):
        g2_point(wrong, wrong)


# --- the pairing boundary ----------------------------------------------------


def test_the_pairing_refuses_an_off_curve_argument() -> None:
    x, y = G1
    off_g1 = (x, (y + 1) % FIELD_MODULUS)
    with pytest.raises(ValueError, match="not on the BLS12-381 curve"):
        pairing(G2, off_g1)

    gx, gy = G2
    off_g2 = (gx, gy + FQ2([1, 0]))
    with pytest.raises(ValueError, match="not on the BLS12-381 twist"):
        pairing(off_g2, G1)


def test_the_pairing_refuses_a_small_subgroup_argument() -> None:
    with pytest.raises(ValueError, match="prime-order subgroup G1"):
        pairing(G2, _small_subgroup_g1())
    with pytest.raises(ValueError, match="prime-order subgroup G2"):
        pairing(_small_subgroup_g2(), G1)


def test_validation_is_on_by_default_and_opt_out_only() -> None:
    """
    The safe behaviour is the default; skipping it takes an explicit keyword.

    A caller who has already validated a fixed key can pass ``validate=False``
    in a loop, and gets the same value -- so the escape hatch is a performance
    choice, never a semantic one.
    """
    assert pairing(G2, G1, validate=False) == pairing(G2, G1)


# --- the identity, decided once and applied to both groups ------------------


def test_the_identity_is_treated_identically_in_g1_and_g2() -> None:
    """
    The identity is on both curves and in both subgroups; only the pairing
    refuses it, and it refuses it in either argument.

    The policy is written down here because "is O a valid point" answered
    differently in G1 and G2 is how a check gets bypassed by passing the
    identity to whichever side is laxer.
    """
    assert is_on_curve_g1(None) and is_in_subgroup_g1(None)
    assert is_on_curve_g2(None) and is_in_subgroup_g2(None)
    validate_g1(None)
    validate_g2(None)

    with pytest.raises(ValueError, match="identity"):
        pairing(None, G1)
    with pytest.raises(ValueError, match="identity"):
        pairing(G2, None)
