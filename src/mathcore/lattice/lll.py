"""
Lattice basis reduction by the Lenstra-Lenstra-Lovasz algorithm.

Owns the ``lattice-reduction-lll`` registry entry.

LLL takes a basis of an integer lattice and returns another basis of the *same*
lattice whose vectors are short and close to orthogonal. That is the whole
engine behind biased-nonce key recovery: a set of ECDSA signatures with partly
known nonces becomes a lattice in which the private key is an unusually short
vector, and LLL finds short vectors.

**Exact arithmetic, on purpose.** The Gram-Schmidt coefficients here are
:class:`~fractions.Fraction`, not floats. A floating-point LLL is much faster
and is what production libraries ship, but it can loop forever or return a
basis that fails its own reduction predicate when the coefficients get close
to the swap threshold. This module is a correctness reference for an attack
whose output is a claim about someone's private key; a "probably reduced"
basis is not good enough to make that claim from. The cost is polynomial and
the dimensions this project reduces are small.

The transformation matrix is available from :func:`lll_reduce_with_transform`,
which is what makes "same lattice" checkable rather than assumed: the matrix is
unimodular exactly when the two bases span the same lattice.

References: Lenstra, Lenstra & Lovasz, *Factoring polynomials with rational
coefficients*, Math. Ann. 261 (1982); Cohen, *A Course in Computational
Algebraic Number Theory*, alg. 2.6.3; Galbraith, *Mathematics of Public Key
Cryptography*, ch. 17.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

__all__ = [
    "DEFAULT_DELTA",
    "gram_schmidt",
    "is_lll_reduced",
    "lll_reduce",
    "lll_reduce_with_transform",
]


# The classical Lovasz parameter. Larger delta gives a better-reduced basis and
# a slower reduction; 3/4 is what the original paper uses and what the standard
# polynomial-time bound is proved for.
DEFAULT_DELTA = Fraction(3, 4)

Vector = list[Fraction]
Matrix = list[list[Fraction]]


def _dot(u: Sequence[Fraction], v: Sequence[Fraction]) -> Fraction:
    return sum((a * b for a, b in zip(u, v, strict=True)), Fraction(0))


def gram_schmidt(basis: Sequence[Sequence[int]]) -> tuple[Matrix, Matrix]:
    """
    Exact Gram-Schmidt orthogonalisation of an integer basis.

    Returns ``(orthogonal, mu)`` where ``orthogonal[i]`` is the component of
    ``basis[i]`` orthogonal to everything before it, and ``mu[i][j]`` is the
    projection coefficient of ``basis[i]`` onto ``orthogonal[j]``.

    Raises :class:`ValueError` if the rows are linearly dependent. LLL is
    defined for a basis, and a dependent set has no Gram-Schmidt: the
    orthogonalisation produces a zero vector and the next projection divides by
    its zero norm. Detecting it here gives a caller a sentence rather than a
    ``ZeroDivisionError`` from three frames down.
    """
    rows = [[Fraction(x) for x in row] for row in basis]
    orthogonal: Matrix = []
    mu: Matrix = []
    for i, row in enumerate(rows):
        coefficients = [Fraction(0)] * len(rows)
        current = list(row)
        for j, previous in enumerate(orthogonal):
            norm = _dot(previous, previous)
            if norm == 0:  # pragma: no cover - guarded by the check below
                raise ValueError("linearly dependent basis")
            coefficients[j] = _dot(row, previous) / norm
            current = [c - coefficients[j] * p for c, p in zip(current, previous, strict=True)]
        if _dot(current, current) == 0:
            raise ValueError(
                f"row {i} is a linear combination of the rows before it; LLL "
                "reduces a basis, and a dependent set is not one"
            )
        orthogonal.append(current)
        mu.append(coefficients)
    return orthogonal, mu


def is_lll_reduced(basis: Sequence[Sequence[int]], delta: Fraction = DEFAULT_DELTA) -> bool:
    """
    Whether ``basis`` satisfies the two LLL conditions for ``delta``.

    Size reduction: ``|mu[i][j]| <= 1/2`` for every ``j < i``. Lovasz:
    ``||b*_i||**2 >= (delta - mu[i][i-1]**2) * ||b*_(i-1)||**2``.

    This is the definition, written separately from the algorithm so the tests
    can check the output against the specification rather than against the
    implementation that produced it. Not constant time; nothing here is.
    """
    _validate_delta(delta)
    orthogonal, mu = gram_schmidt(basis)
    for i in range(len(basis)):
        for j in range(i):
            if abs(mu[i][j]) > Fraction(1, 2):
                return False
    for i in range(1, len(basis)):
        left = _dot(orthogonal[i], orthogonal[i])
        right = (delta - mu[i][i - 1] ** 2) * _dot(orthogonal[i - 1], orthogonal[i - 1])
        if left < right:
            return False
    return True


def lll_reduce(basis: Sequence[Sequence[int]], delta: Fraction = DEFAULT_DELTA) -> list[list[int]]:
    """
    Return an LLL-reduced basis of the same lattice.

    The input rows are the basis vectors. The output spans exactly the same
    lattice -- not an approximation of it -- and satisfies
    :func:`is_lll_reduced` for the same ``delta``.

    Not constant time, and not intended to be: the inputs are public lattices
    built from public signature data.
    """
    return lll_reduce_with_transform(basis, delta)[0]


def lll_reduce_with_transform(
    basis: Sequence[Sequence[int]], delta: Fraction = DEFAULT_DELTA
) -> tuple[list[list[int]], list[list[int]]]:
    """
    Reduce, and also return the unimodular matrix that did it.

    Returns ``(reduced, transform)`` with ``transform @ basis == reduced``. The
    matrix is unimodular -- integer entries, determinant ``+-1`` -- and that is
    precisely the statement that the two bases span the same lattice. Returning
    it turns "same lattice" from a property the caller must trust into one they
    can check, which for an attack tool is the difference between a result and
    an assertion.
    """
    _validate_delta(delta)
    rows = [list(map(int, row)) for row in basis]
    _validate_shape(rows)

    dimension = len(rows)
    transform = [[1 if i == j else 0 for j in range(dimension)] for i in range(dimension)]
    gram_schmidt(rows)  # fail fast on a dependent basis, before any mutation

    k = 1
    while k < dimension:
        orthogonal, mu = gram_schmidt(rows)
        for j in range(k - 1, -1, -1):
            if abs(mu[k][j]) > Fraction(1, 2):
                factor = _nearest_integer(mu[k][j])
                rows[k] = [a - factor * b for a, b in zip(rows[k], rows[j], strict=True)]
                transform[k] = [
                    a - factor * b for a, b in zip(transform[k], transform[j], strict=True)
                ]
                orthogonal, mu = gram_schmidt(rows)

        left = _dot(orthogonal[k], orthogonal[k])
        right = (delta - mu[k][k - 1] ** 2) * _dot(orthogonal[k - 1], orthogonal[k - 1])
        if left >= right:
            k += 1
        else:
            rows[k], rows[k - 1] = rows[k - 1], rows[k]
            transform[k], transform[k - 1] = transform[k - 1], transform[k]
            k = max(k - 1, 1)

    return rows, transform


def _nearest_integer(value: Fraction) -> int:
    """
    Round a Fraction to the nearest integer, halves away from zero.

    Python's ``round`` on a ``Fraction`` rounds halves to even, which also
    keeps ``|mu| <= 1/2`` and so is equally valid. The rule is spelled out here
    anyway: a reader checking this against a textbook should not have to work
    out whether the tie-break matters, and the answer is that it does not.
    """
    floor = value.numerator // value.denominator
    remainder = value - floor
    if remainder > Fraction(1, 2):
        return floor + 1
    if remainder == Fraction(1, 2):
        return floor + 1 if value > 0 else floor
    return floor


def _validate_delta(delta: Fraction) -> None:
    """
    Check the Lovasz parameter.

    ``delta`` must lie in ``(1/4, 1]``. At or below ``1/4`` the swap condition
    stops guaranteeing progress and the loop need not terminate; above ``1``
    it is unsatisfiable and the loop swaps forever. Neither failure announces
    itself -- both simply hang -- so they are refused here.
    """
    if not isinstance(delta, Fraction):
        raise TypeError(f"delta must be a Fraction, got {type(delta).__name__}")
    if not Fraction(1, 4) < delta <= 1:
        raise ValueError(f"delta must lie in (1/4, 1], got {delta}")


def _validate_shape(rows: list[list[int]]) -> None:
    """Check the basis is a non-empty rectangular integer matrix."""
    if not rows:
        raise ValueError("cannot reduce an empty basis")
    width = len(rows[0])
    if width == 0:
        raise ValueError("cannot reduce a basis of zero-length vectors")
    if any(len(row) != width for row in rows):
        raise ValueError("every basis vector must have the same length")
    if len(rows) > width:
        raise ValueError(
            f"{len(rows)} vectors of length {width} cannot be independent; "
            "LLL reduces a basis, not a spanning set"
        )
