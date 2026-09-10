"""
Module-lattice algebra and the smoothing parameter behind every LWE proof.

Owns two registry entries:

* ``lattices-module-algebra`` -- arithmetic in the module ``(Z_q[x]/(x^n+1))^k``
  that ML-KEM and ML-DSA are built on. The ring multiplication is the NTT from
  :mod:`src.mathcore.fields.ntt`; this module adds the vector-and-matrix layer
  on top, which is where Module-LWE actually lives.
* ``poisson-summation-smoothing`` -- the smoothing parameter ``eta_epsilon``,
  the width above which a discrete Gaussian on a lattice behaves like the
  continuous one. Poisson summation is the identity that proves it, and every
  worst-case-to-average-case lattice reduction depends on the error being
  sampled above this width.

The load-bearing function here is :func:`assert_width_above_smoothing`. Choosing
a Gaussian width below the smoothing parameter does not break any functional
test -- keys still agree, ciphertexts still decrypt -- it silently voids the
security proof. That is the failure this module is built to make visible, so it
is a check a caller can run, not a fact buried in a paper.

Deterministic float and integer arithmetic on public structural parameters;
constant-time does not apply. This is a correctness and parameter-audit
reference, not the sampler on a signing hot path.

References: Micciancio & Regev, *Worst-case to average-case reductions based on
Gaussian measures* (2007), for the smoothing parameter; Lyubashevsky, Peikert &
Regev, *On Ideal Lattices and Learning with Errors over Rings* (2013); FIPS 203.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from ..fields.ntt import ML_KEM_N, ML_KEM_Q, intt, ntt, ntt_multiply

__all__ = [
    "ModuleElement",
    "smoothing_parameter",
    "assert_width_above_smoothing",
    "is_above_smoothing",
    "module_matrix_vector",
]


def smoothing_parameter(dimension: int, epsilon: float) -> float:
    """
    The smoothing parameter ``eta_epsilon(Z^n)`` for the integer lattice.

    Uses the standard bound ``eta_epsilon(Z^n) <= sqrt(ln(2n(1 + 1/epsilon)) /
    pi)`` (Micciancio-Regev): above this width a discrete Gaussian's Fourier
    transform is within ``epsilon`` of the continuous Gaussian's, which is
    exactly the condition the security reductions invoke. ``pi`` appears here
    structurally, from the Gaussian's own normalisation -- not as a fitted
    constant -- which is the honest use ``pi-structural-uses`` refers to.

    Smaller ``epsilon`` (a tighter approximation) gives a larger parameter, as
    it must: demanding the discrete and continuous Gaussians agree more closely
    forces a wider distribution. ``epsilon`` must be in ``(0, 1)`` and
    ``dimension`` at least 1. Not constant time.
    """
    if dimension < 1:
        raise ValueError(f"dimension must be at least 1, got {dimension}")
    if not 0 < epsilon < 1:
        raise ValueError(f"epsilon must be in (0, 1), got {epsilon}")
    return math.sqrt(math.log(2 * dimension * (1 + 1 / epsilon)) / math.pi)


def is_above_smoothing(sigma: float, dimension: int, epsilon: float) -> bool:
    """
    Whether Gaussian width ``sigma`` is at or above ``eta_epsilon(Z^n)``.

    The comparison the security proof needs, as a plain predicate. Not constant
    time.
    """
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")
    return sigma >= smoothing_parameter(dimension, epsilon)


def assert_width_above_smoothing(sigma: float, dimension: int, epsilon: float = 2.0**-64) -> None:
    """
    Refuse a Gaussian width below the smoothing parameter.

    The load-bearing check of this module. A width below ``eta_epsilon`` leaves
    every functional test green -- keys agree, ciphertexts decrypt -- while the
    worst-case-to-average-case reduction no longer holds, so the scheme keeps
    its interface and loses its proof with no visible symptom. This turns that
    silent condition into a loud one.

    The default ``epsilon`` of ``2^-64`` is the cryptographic setting: the
    approximation must be tight enough that the security loss is negligible over
    the whole parameter space, not merely small. Raises :class:`ValueError`
    naming both the supplied width and the required one. Not constant time.
    """
    required = smoothing_parameter(dimension, epsilon)
    if sigma < required:
        raise ValueError(
            f"Gaussian width {sigma:.4f} is below the smoothing parameter "
            f"{required:.4f} for dimension {dimension} at epsilon={epsilon:g}: "
            "sampling here voids the LWE security reduction while leaving every "
            "functional test passing. Widen the distribution or raise epsilon "
            "only with a reason."
        )


class ModuleElement:
    """
    A vector of ``k`` polynomials in ``Z_q[x]/(x^256+1)`` -- an element of the
    Module-LWE module over the ML-KEM ring.

    Addition is componentwise; the inner product with another module element
    contracts to a single ring element, via the NTT for the polynomial
    products. Stored as coefficient-domain polynomials; the NTT is applied
    internally for multiplication so a caller need not track which domain a
    value is in. Not constant time.
    """

    __slots__ = ("polynomials",)

    def __init__(self, polynomials: Sequence[Sequence[int]]) -> None:
        polys = [list(p) for p in polynomials]
        if not polys:
            raise ValueError("a module element needs at least one polynomial")
        for p in polys:
            if len(p) != ML_KEM_N:
                raise ValueError(f"each polynomial needs {ML_KEM_N} coefficients, got {len(p)}")
        self.polynomials = [[c % ML_KEM_Q for c in p] for p in polys]

    @property
    def rank(self) -> int:
        """The number of polynomial components ``k``. Not constant time."""
        return len(self.polynomials)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModuleElement):
            return NotImplemented
        return self.polynomials == other.polynomials

    def add(self, other: ModuleElement) -> ModuleElement:
        """Componentwise sum. Rank must match. Not constant time."""
        if self.rank != other.rank:
            raise ValueError(f"rank mismatch: {self.rank} vs {other.rank}")
        return ModuleElement(
            [
                [(a + b) % ML_KEM_Q for a, b in zip(p, q, strict=True)]
                for p, q in zip(self.polynomials, other.polynomials, strict=True)
            ]
        )

    def inner_product(self, other: ModuleElement) -> list[int]:
        """
        The ring element ``sum_i self_i * other_i``, products via the NTT.

        Contracts a length-``k`` vector against another to one polynomial, the
        operation at the heart of a Module-LWE sample. Rank must match. Returns
        coefficient-domain. Not constant time.
        """
        if self.rank != other.rank:
            raise ValueError(f"rank mismatch: {self.rank} vs {other.rank}")
        accumulator = [0] * ML_KEM_N
        for p, q in zip(self.polynomials, other.polynomials, strict=True):
            product = intt(ntt_multiply(ntt(p), ntt(q)))
            accumulator = [(a + b) % ML_KEM_Q for a, b in zip(accumulator, product, strict=True)]
        return accumulator


def module_matrix_vector(
    matrix: Sequence[Sequence[Sequence[int]]], vector: ModuleElement
) -> ModuleElement:
    """
    Multiply a ``k x k`` matrix of ring elements by a module vector.

    ``matrix[i][j]`` is a polynomial; row ``i`` of the result is
    ``sum_j matrix[i][j] * vector_j``. This is the ``A s`` that generates a
    Module-LWE public key. Every ring product goes through the NTT. Not constant
    time.
    """
    rows = [list(row) for row in matrix]
    if not rows:
        raise ValueError("matrix must have at least one row")
    for row in rows:
        if len(row) != vector.rank:
            raise ValueError(
                f"matrix row width {len(row)} does not match vector rank {vector.rank}"
            )
    result_rows = []
    for row in rows:
        row_element = ModuleElement(row)
        result_rows.append(row_element.inner_product(vector))
    return ModuleElement(result_rows)
