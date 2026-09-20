"""
Tests for :mod:`src.mathcore.curves.bls12_381`.

The pairing's whole purpose is one identity, so that is the headline test:
bilinearity, e(a*G2, b*G1) == e(G2, G1)^(a*b). Non-degeneracy (the pairing of
the generators is not 1, and has order r) is checked too, since a "pairing"
that collapsed to 1 would satisfy nothing useful. Pairings are slow in pure
Python, so the count is kept small; an independent implementation (py_ecc, when
installed) pins the exact value as an extra oracle.
"""

from __future__ import annotations

import pytest

from src.mathcore.curves.bls12_381 import (
    CURVE_ORDER,
    G1,
    G2,
    g1_multiply,
    g2_multiply,
    pairing,
)
from src.mathcore.curves.bls12_381 import FIELD_MODULUS as _FIELD


def test_the_pairing_is_bilinear() -> None:
    """e(a*G2, b*G1) == e(G2, G1)^(a*b) -- the identity every use relies on."""
    base = pairing(G2, G1)
    assert pairing(g2_multiply(2), g1_multiply(3)) == base**6
    assert pairing(g2_multiply(5), g1_multiply(7)) == base**35


def test_bilinear_in_each_argument_separately() -> None:
    base = pairing(G2, G1)
    assert pairing(g2_multiply(4), G1) == base**4
    assert pairing(G2, g1_multiply(4)) == base**4


def test_the_pairing_is_non_degenerate() -> None:
    base = pairing(G2, G1)
    assert base != base**0  # not the identity
    assert base**CURVE_ORDER == base**0  # but has order dividing r


def test_pairing_rejects_the_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        pairing(None, G1)
    with pytest.raises(ValueError, match="identity"):
        pairing(G2, None)


def test_generators_have_the_right_order() -> None:
    assert g1_multiply(CURVE_ORDER) is None
    assert g2_multiply(CURVE_ORDER) is None
    assert g1_multiply(CURVE_ORDER + 1) == G1
    assert g2_multiply(CURVE_ORDER + 1) == G2


def test_scalar_multiplication_is_additive() -> None:
    from src.mathcore.curves.bls12_381 import _add

    assert _add(g1_multiply(3), g1_multiply(4)) == g1_multiply(7)
    assert _add(g2_multiply(3), g2_multiply(4)) == g2_multiply(7)


def test_matches_an_independent_implementation() -> None:
    """py_ecc, when available, fixes the exact pairing value byte-for-byte."""
    pytest.importorskip("py_ecc")
    from py_ecc.bls12_381 import bls12_381_curve as c
    from py_ecc.bls12_381 import bls12_381_pairing as p

    reference = [int(x) for x in p.pairing(c.G2, c.G1).coeffs]
    assert pairing(G2, G1).coeffs == reference


def test_pairing_of_multiplied_points_matches_reference() -> None:
    pytest.importorskip("py_ecc")
    from py_ecc.bls12_381 import bls12_381_curve as c
    from py_ecc.bls12_381 import bls12_381_pairing as p

    reference = [int(x) for x in p.pairing(c.multiply(c.G2, 3), c.multiply(c.G1, 2)).coeffs]
    assert pairing(g2_multiply(3), g1_multiply(2)).coeffs == reference


# ---- internal arithmetic edge cases (fast: no pairing) ---------------------


def test_fqp_guards_and_operators() -> None:
    from src.mathcore.curves.bls12_381 import FQ2

    with pytest.raises(ValueError, match="degree"):
        FQ2([1, 2, 3])  # wrong coefficient count
    a = FQ2([3, 4])
    b = FQ2([1, 1])
    assert (a - b) == FQ2([2, 3])  # __sub__
    assert (-a).coeffs == [(-3) % _FIELD, (-4) % _FIELD]  # __neg__
    assert (a == "not a field element") is False  # __eq__ with a foreign type


def test_double_edge_cases() -> None:
    from src.mathcore.curves.bls12_381 import _double

    assert _double(None) is None
    assert _double((5, 0)) is None  # int branch, y == 0 -> infinity


def test_add_edge_cases() -> None:
    from src.mathcore.curves.bls12_381 import FIELD_MODULUS, _add, _double

    assert _add(None, G1) == G1
    assert _add(G1, None) == G1
    # int branch: point plus its own inverse is the identity
    x, y = G1
    assert _add(G1, (x, (-y) % FIELD_MODULUS)) is None
    # int branch: equal points route through double
    assert _add(G1, G1) == _double(G1)
    # FQP branch: point plus its inverse, and equal points
    gx, gy = G2
    assert _add(G2, (gx, -gy)) is None
    assert _add(G2, G2) == _double(G2)


def test_multiply_by_zero_is_the_identity() -> None:
    from src.mathcore.curves.bls12_381 import _multiply

    assert _multiply(G1, 0) is None
    assert _multiply(G2, 0) is None


def test_linefunc_vertical_line() -> None:
    """When the two points are inverses, the line through them is vertical."""
    from src.mathcore.curves.bls12_381 import _cast_g1, _linefunc, _twist

    q = _twist(G2)
    p = _cast_g1(G1)
    qx, qy = q
    # linefunc(R, -R, p) hits the vertical-line branch (x equal, y differ).
    result = _linefunc(q, (qx, -qy), p)
    assert result is not None


def test_miller_loop_identity_short_circuits() -> None:
    from src.mathcore.curves.bls12_381 import _fq12_one, _miller_loop

    assert _miller_loop(None, None) == _fq12_one()


def test_fqp_addition() -> None:
    """The field's __add__ operator, exercised directly."""
    from src.mathcore.curves.bls12_381 import FQ2

    assert (FQ2([1, 2]) + FQ2([3, 4])) == FQ2([4, 6])


def test_double_of_a_two_torsion_fqp_point_is_infinity() -> None:
    """
    An Fp2 point with zero y doubles to infinity. No such point exists in the
    prime-order subgroup, so it is a defensive guard reached only by a
    synthetic point -- constructed here to exercise it.
    """
    from src.mathcore.curves.bls12_381 import FQ2, _double

    assert _double((FQ2([5, 6]), FQ2([0, 0]))) is None


def test_twist_of_infinity_is_infinity() -> None:
    from src.mathcore.curves.bls12_381 import _twist

    assert _twist(None) is None
