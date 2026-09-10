"""
Number-theoretic primitives for :mod:`mathcore`.

``residues`` owns the Legendre/Jacobi symbols and Tonelli-Shanks square roots;
``primality`` owns Baillie-PSW and the Lucas sequences under it. ``crt``, the
remaining phase-1 module in ``docs/MATH_ROADMAP.md``, is not built yet; the
registry, not this file list, is the record of what exists.
"""

from .primality import (
    SMALL_PRIMES,
    is_probable_prime,
    lucas_sequence,
    selfridge_parameters,
    strong_lucas_probable_prime,
    strong_probable_prime,
)
from .residues import (
    is_quadratic_residue,
    jacobi_symbol,
    legendre_symbol,
    sqrt_mod_prime,
)

__all__ = [
    "SMALL_PRIMES",
    "is_probable_prime",
    "is_quadratic_residue",
    "jacobi_symbol",
    "legendre_symbol",
    "lucas_sequence",
    "selfridge_parameters",
    "sqrt_mod_prime",
    "strong_lucas_probable_prime",
    "strong_probable_prime",
]
