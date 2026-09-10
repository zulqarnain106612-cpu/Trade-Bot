"""
Tests for :mod:`src.mathcore.harmonic.gaussian_lattice`.

Two entries. The smoothing parameter is checked for the monotonicity the
security argument relies on and for the load-bearing refusal of an under-width
Gaussian -- the silent failure the roadmap names. The module algebra is checked
against the ring it is built on: an identity matrix returns the vector, and the
inner product agrees with schoolbook negacyclic convolution summed over
components.
"""

from __future__ import annotations

import math
import random

import pytest

from src.mathcore.fields.ntt import ML_KEM_N, ML_KEM_Q, negacyclic_convolution
from src.mathcore.harmonic.gaussian_lattice import (
    ModuleElement,
    assert_width_above_smoothing,
    is_above_smoothing,
    module_matrix_vector,
    smoothing_parameter,
)


def _rp(rng: random.Random) -> list[int]:
    return [rng.randrange(ML_KEM_Q) for _ in range(ML_KEM_N)]


# ---- smoothing parameter ---------------------------------------------------


def test_a_tighter_epsilon_demands_a_wider_gaussian() -> None:
    """Smaller epsilon (closer to continuous) forces a larger parameter."""
    values = [smoothing_parameter(256, eps) for eps in (0.5, 0.1, 0.01, 1e-6, 2.0**-64)]
    assert all(a < b for a, b in zip(values, values[1:], strict=False))


def test_a_higher_dimension_raises_the_parameter() -> None:
    assert smoothing_parameter(256, 0.01) < smoothing_parameter(1024, 0.01)


def test_the_parameter_matches_the_micciancio_regev_formula() -> None:
    n, eps = 256, 2.0**-64
    expected = math.sqrt(math.log(2 * n * (1 + 1 / eps)) / math.pi)
    assert smoothing_parameter(n, eps) == pytest.approx(expected)


def test_is_above_smoothing_agrees_with_the_parameter() -> None:
    eta = smoothing_parameter(256, 0.01)
    assert is_above_smoothing(eta + 0.1, 256, 0.01)
    assert not is_above_smoothing(eta - 0.1, 256, 0.01)


def test_an_under_width_gaussian_is_refused_loudly() -> None:
    """The load-bearing check: a width below the parameter voids the proof."""
    with pytest.raises(ValueError, match="voids the LWE security reduction"):
        assert_width_above_smoothing(1.0, 256)


def test_a_sufficient_width_passes() -> None:
    assert_width_above_smoothing(5.0, 256)  # eta(256, 2^-64) ~ 4.01


@pytest.mark.parametrize("bad_eps", [0.0, 1.0, 1.5, -0.1])
def test_an_epsilon_outside_the_open_unit_interval_is_refused(bad_eps: float) -> None:
    with pytest.raises(ValueError, match="epsilon"):
        smoothing_parameter(256, bad_eps)


def test_a_degenerate_dimension_is_refused() -> None:
    with pytest.raises(ValueError, match="dimension"):
        smoothing_parameter(0, 0.01)


def test_a_nonpositive_width_is_refused() -> None:
    with pytest.raises(ValueError, match="sigma"):
        is_above_smoothing(0.0, 256, 0.01)


# ---- module algebra --------------------------------------------------------


def test_the_identity_matrix_returns_the_vector() -> None:
    rng = random.Random("identity")
    s = ModuleElement([_rp(rng), _rp(rng)])
    identity = [
        [[1] + [0] * 255, [0] * 256],
        [[0] * 256, [1] + [0] * 255],
    ]
    assert module_matrix_vector(identity, s) == s


def test_inner_product_agrees_with_schoolbook_convolution() -> None:
    rng = random.Random("inner")
    a = ModuleElement([_rp(rng), _rp(rng), _rp(rng)])
    b = ModuleElement([_rp(rng), _rp(rng), _rp(rng)])
    expected = [0] * ML_KEM_N
    for p, q in zip(a.polynomials, b.polynomials, strict=True):
        term = negacyclic_convolution(p, q)
        expected = [(x + y) % ML_KEM_Q for x, y in zip(expected, term, strict=True)]
    assert a.inner_product(b) == expected


def test_addition_is_componentwise() -> None:
    rng = random.Random("add")
    a = ModuleElement([_rp(rng), _rp(rng)])
    b = ModuleElement([_rp(rng), _rp(rng)])
    summed = a.add(b)
    for i in range(a.rank):
        assert summed.polynomials[i] == [
            (x + y) % ML_KEM_Q for x, y in zip(a.polynomials[i], b.polynomials[i], strict=True)
        ]


def test_rank_is_the_component_count() -> None:
    assert ModuleElement([[0] * 256, [0] * 256, [0] * 256]).rank == 3


def test_matrix_vector_reproduces_a_hand_computed_product() -> None:
    """
    A 2x2 matrix of scalars (constant polynomials) times a scalar vector is
    ordinary matrix arithmetic mod q, which is checkable by hand.
    """

    def const(c: int) -> list[int]:
        return [c] + [0] * 255

    matrix = [[const(1), const(2)], [const(3), const(4)]]
    vector = ModuleElement([const(5), const(6)])
    result = module_matrix_vector(matrix, vector)
    assert result.polynomials[0][0] == (1 * 5 + 2 * 6) % ML_KEM_Q
    assert result.polynomials[1][0] == (3 * 5 + 4 * 6) % ML_KEM_Q


# ---- validation ------------------------------------------------------------


def test_rank_mismatch_is_refused() -> None:
    a = ModuleElement([[0] * 256])
    b = ModuleElement([[0] * 256, [0] * 256])
    with pytest.raises(ValueError, match="rank mismatch"):
        a.add(b)
    with pytest.raises(ValueError, match="rank mismatch"):
        a.inner_product(b)


def test_a_wrong_polynomial_length_is_refused() -> None:
    with pytest.raises(ValueError, match="256 coefficients"):
        ModuleElement([[1, 2, 3]])


def test_an_empty_module_element_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one polynomial"):
        ModuleElement([])


def test_an_empty_matrix_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        module_matrix_vector([], ModuleElement([[0] * 256]))


def test_a_matrix_row_of_wrong_width_is_refused() -> None:
    vector = ModuleElement([[0] * 256, [0] * 256])
    with pytest.raises(ValueError, match="does not match vector rank"):
        module_matrix_vector([[[0] * 256]], vector)


def test_module_elements_compare_by_value() -> None:
    a = ModuleElement([[1] + [0] * 255])
    b = ModuleElement([[1] + [0] * 255])
    assert a == b
    assert (a == "not a module element") is False
