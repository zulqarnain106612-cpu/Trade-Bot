"""
Number-theoretic primitives for :mod:`mathcore`.

``residues`` owns the Legendre/Jacobi symbols and Tonelli-Shanks square roots.
Further modules named by ``docs/MATH_ROADMAP.md`` phase 1 -- ``primality`` and
``crt`` -- are not built yet; the registry, not this file list, is the record
of what exists.
"""

from .residues import (
    is_quadratic_residue,
    jacobi_symbol,
    legendre_symbol,
    sqrt_mod_prime,
)

__all__ = [
    "is_quadratic_residue",
    "jacobi_symbol",
    "legendre_symbol",
    "sqrt_mod_prime",
]
