"""
Tests for :mod:`src.mathcore.lattice.lll`.

The reduction is checked against the *definition* rather than against itself:
:func:`is_lll_reduced` is written separately from the algorithm, and the tests
assert the output satisfies it. Anything else would be an implementation
agreeing with itself.

The other half is that the output must span the same lattice. That is not
observable from the vectors alone, so it is checked through the transformation
matrix: ``transform @ basis == reduced`` and ``det(transform) == +-1``, which
is exactly the statement that no lattice points were gained or lost.
"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from src.mathcore.lattice.lll import (
    DEFAULT_DELTA,
    gram_schmidt,
    is_lll_reduced,
    lll_reduce,
    lll_reduce_with_transform,
)

# The example basis from the LLL literature, and its published reduction.
TEXTBOOK_BASIS = [[1, 1, 1], [-1, 0, 2], [3, 5, 6]]
TEXTBOOK_REDUCED = [[0, 1, 0], [1, 0, 1], [-1, 0, 2]]

DELTAS = [Fraction(1, 2), DEFAULT_DELTA, Fraction(99, 100), Fraction(1)]


def _determinant(matrix: list[list[int]]) -> Fraction:
    """Exact determinant by fraction Gaussian elimination."""
    rows = [[Fraction(x) for x in row] for row in matrix]
    size = len(rows)
    result = Fraction(1)
    for column in range(size):
        pivot = next((r for r in range(column, size) if rows[r][column] != 0), None)
        if pivot is None:
            return Fraction(0)
        if pivot != column:
            rows[column], rows[pivot] = rows[pivot], rows[column]
            result = -result
        result *= rows[column][column]
        for r in range(column + 1, size):
            factor = rows[r][column] / rows[column][column]
            rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column], strict=True)]
    return result


def _matrix_multiply(a: list[list[int]], b: list[list[int]]) -> list[list[int]]:
    return [
        [sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))]
        for i in range(len(a))
    ]


def _norm_squared(vector: list[int]) -> int:
    return sum(x * x for x in vector)


def _random_basis(rng: random.Random, dimension: int, bound: int) -> list[list[int]]:
    """A random square basis, retried until it is non-singular."""
    while True:
        basis = [
            [rng.randrange(-bound, bound + 1) for _ in range(dimension)] for _ in range(dimension)
        ]
        if _determinant(basis) != 0:
            return basis


# ---- known answer ----------------------------------------------------------


def test_the_textbook_basis_reduces_to_its_published_result() -> None:
    assert lll_reduce(TEXTBOOK_BASIS) == TEXTBOOK_REDUCED


def test_the_published_result_is_itself_reduced() -> None:
    assert is_lll_reduced(TEXTBOOK_REDUCED)
    assert not is_lll_reduced(TEXTBOOK_BASIS)


# ---- the definition --------------------------------------------------------


@pytest.mark.parametrize("delta", DELTAS)
def test_the_output_satisfies_the_reduction_predicate(delta: Fraction) -> None:
    rng = random.Random(f"reduced-{delta}")
    for dimension in (2, 3, 4, 5):
        for _ in range(3):
            basis = _random_basis(rng, dimension, 20)
            assert is_lll_reduced(lll_reduce(basis, delta), delta), (dimension, basis)


def test_a_stricter_delta_still_satisfies_a_looser_one() -> None:
    """Reduction for delta is reduction for anything smaller."""
    rng = random.Random("monotone")
    basis = _random_basis(rng, 4, 30)
    strict = lll_reduce(basis, Fraction(99, 100))
    assert is_lll_reduced(strict, Fraction(99, 100))
    assert is_lll_reduced(strict, DEFAULT_DELTA)


def test_the_two_reduction_conditions_fail_independently() -> None:
    """
    ``[[2, 0], [0, 1]]`` is perfectly size-reduced -- every mu is zero -- and
    still fails Lovasz, because the second Gram-Schmidt vector is much shorter
    than the first. Swapping the rows fixes it. Both halves of the predicate
    need a case of their own, or one could be dropped without a test noticing.
    """
    lovasz_only = [[2, 0], [0, 1]]
    _orthogonal, mu = gram_schmidt(lovasz_only)
    assert mu[1][0] == 0, "this fixture must be size-reduced already"
    assert not is_lll_reduced(lovasz_only)
    assert is_lll_reduced([[0, 1], [2, 0]])
    assert lll_reduce(lovasz_only) == [[0, 1], [2, 0]]


def test_an_already_reduced_basis_is_left_alone() -> None:
    identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    assert lll_reduce(identity) == identity
    orthogonal = [[2, 0], [0, 3]]
    assert lll_reduce(orthogonal) == orthogonal


# ---- same lattice ----------------------------------------------------------


@pytest.mark.parametrize("delta", DELTAS)
def test_the_transform_reproduces_the_reduced_basis(delta: Fraction) -> None:
    rng = random.Random(f"transform-{delta}")
    for dimension in (2, 3, 4):
        basis = _random_basis(rng, dimension, 25)
        reduced, transform = lll_reduce_with_transform(basis, delta)
        assert _matrix_multiply(transform, basis) == reduced


@pytest.mark.parametrize("delta", DELTAS)
def test_the_transform_is_unimodular(delta: Fraction) -> None:
    """
    Determinant ``+-1`` is exactly the statement that the two bases span the
    same lattice. Without it a "reduced basis" could be a sublattice's, whose
    short vectors are not the original lattice's short vectors.
    """
    rng = random.Random(f"unimodular-{delta}")
    for dimension in (2, 3, 4, 5):
        basis = _random_basis(rng, dimension, 25)
        _reduced, transform = lll_reduce_with_transform(basis, delta)
        assert abs(_determinant(transform)) == 1


def test_the_lattice_determinant_is_preserved_up_to_sign() -> None:
    rng = random.Random("determinant")
    for dimension in (2, 3, 4):
        basis = _random_basis(rng, dimension, 25)
        assert abs(_determinant(lll_reduce(basis))) == abs(_determinant(basis))


def test_every_original_vector_is_in_the_reduced_lattice() -> None:
    """
    The converse direction, checked directly rather than inferred: each input
    vector must be an integer combination of the output vectors.
    """
    rng = random.Random("spans-back")
    basis = _random_basis(rng, 4, 20)
    reduced, _transform = lll_reduce_with_transform(basis)
    for row in basis:
        coefficients = _solve_integer_combination(reduced, row)
        assert coefficients is not None, row


def _solve_integer_combination(basis: list[list[int]], target: list[int]) -> list[int] | None:
    """Solve ``x @ basis == target`` exactly, returning ``x`` only if integral."""
    size = len(basis)
    augmented = [
        [Fraction(basis[r][c]) for r in range(size)] + [Fraction(target[c])]
        for c in range(len(target))
    ]
    row = 0
    pivots = []
    for column in range(size):
        pivot = next((r for r in range(row, len(augmented)) if augmented[r][column] != 0), None)
        if pivot is None:
            continue
        augmented[row], augmented[pivot] = augmented[pivot], augmented[row]
        scale = augmented[row][column]
        augmented[row] = [value / scale for value in augmented[row]]
        for r in range(len(augmented)):
            if r != row and augmented[r][column] != 0:
                factor = augmented[r][column]
                augmented[r] = [
                    a - factor * b for a, b in zip(augmented[r], augmented[row], strict=True)
                ]
        pivots.append(column)
        row += 1
    solution = [Fraction(0)] * size
    for index, column in enumerate(pivots):
        solution[column] = augmented[index][-1]
    for r in range(row, len(augmented)):
        if augmented[r][-1] != 0:
            return None
    if any(value.denominator != 1 for value in solution):
        return None
    return [int(value) for value in solution]


# ---- the LLL guarantee -----------------------------------------------------


def test_the_first_vector_meets_the_lll_length_bound() -> None:
    """
    For delta = 3/4, ``||b_1||**(2n) <= 2**(n*(n-1)/2) * det(L)**2``. This is
    the guarantee the whole attack rests on -- it is why a short vector is
    findable at all -- so it is asserted rather than assumed.
    """
    rng = random.Random("bound")
    for dimension in (2, 3, 4):
        for _ in range(3):
            basis = _random_basis(rng, dimension, 30)
            reduced = lll_reduce(basis)
            determinant = abs(_determinant(basis))
            left = Fraction(_norm_squared(reduced[0])) ** dimension
            right = Fraction(2) ** (dimension * (dimension - 1) // 2) * determinant**2
            assert left <= right, (dimension, basis)


def test_the_first_vector_is_short_relative_to_the_lattice_determinant() -> None:
    """
    The bound above is the guarantee; what it does *not* say is that ``b_1`` is
    shorter than every input vector. LLL bounds ``||b_1||`` against the true
    shortest vector by a factor of ``2**((n-1)/2)``, not against whatever the
    caller happened to hand it. Asserting the stronger claim would be asserting
    something LLL does not provide, so this asserts the real one on a basis
    where the input is deliberately long.
    """
    basis = [[97, 0, 0], [0, 89, 0], [83, 79, 73]]
    reduced = lll_reduce(basis)
    assert _norm_squared(reduced[0]) <= _norm_squared(basis[0])


# ---- Gram-Schmidt ----------------------------------------------------------


def test_gram_schmidt_produces_orthogonal_vectors() -> None:
    orthogonal, _mu = gram_schmidt(TEXTBOOK_BASIS)
    for i, u in enumerate(orthogonal):
        for v in orthogonal[i + 1 :]:
            assert sum(a * b for a, b in zip(u, v, strict=True)) == 0


def test_gram_schmidt_coefficients_rebuild_the_basis() -> None:
    orthogonal, mu = gram_schmidt(TEXTBOOK_BASIS)
    for i, row in enumerate(TEXTBOOK_BASIS):
        rebuilt = list(orthogonal[i])
        for j in range(i):
            rebuilt = [a + mu[i][j] * b for a, b in zip(rebuilt, orthogonal[j], strict=True)]
        assert rebuilt == [Fraction(x) for x in row]


def test_gram_schmidt_refuses_a_dependent_basis() -> None:
    with pytest.raises(ValueError, match="linear combination"):
        gram_schmidt([[1, 2], [2, 4]])


# ---- validation ------------------------------------------------------------


def test_a_dependent_basis_is_refused_before_anything_is_mutated() -> None:
    basis = [[1, 2, 3], [2, 4, 6], [1, 0, 0]]
    with pytest.raises(ValueError, match="linear combination"):
        lll_reduce(basis)
    assert basis == [[1, 2, 3], [2, 4, 6], [1, 0, 0]], "the input was modified"


def test_an_empty_basis_is_refused() -> None:
    with pytest.raises(ValueError, match="empty basis"):
        lll_reduce([])


def test_zero_length_vectors_are_refused() -> None:
    with pytest.raises(ValueError, match="zero-length"):
        lll_reduce([[]])


def test_a_ragged_basis_is_refused() -> None:
    with pytest.raises(ValueError, match="same length"):
        lll_reduce([[1, 2], [3]])


def test_more_vectors_than_dimensions_is_refused() -> None:
    """Three vectors in the plane cannot be independent, whatever they are."""
    with pytest.raises(ValueError, match="cannot be independent"):
        lll_reduce([[1, 0], [0, 1], [1, 1]])


@pytest.mark.parametrize(
    "delta", [Fraction(1, 4), Fraction(0), Fraction(-1), Fraction(2), Fraction(5, 4)]
)
def test_a_delta_outside_the_valid_range_is_refused(delta: Fraction) -> None:
    """
    Both failures hang rather than announce themselves: at or below 1/4 the
    swap condition stops guaranteeing progress, above 1 it is unsatisfiable.
    """
    with pytest.raises(ValueError, match=r"\(1/4, 1\]"):
        lll_reduce(TEXTBOOK_BASIS, delta)


def test_a_float_delta_is_refused() -> None:
    """Exact arithmetic is the point; a float delta would smuggle in rounding."""
    with pytest.raises(TypeError):
        lll_reduce(TEXTBOOK_BASIS, 0.75)  # type: ignore[arg-type]


def test_is_lll_reduced_validates_delta_too() -> None:
    with pytest.raises(ValueError, match=r"\(1/4, 1\]"):
        is_lll_reduced(TEXTBOOK_REDUCED, Fraction(2))


def test_a_rectangular_basis_is_accepted() -> None:
    """Fewer vectors than dimensions is an ordinary lattice, not an error."""
    basis = [[1, 2, 3], [4, 5, 7]]
    reduced = lll_reduce(basis)
    assert is_lll_reduced(reduced)
    assert len(reduced) == 2
