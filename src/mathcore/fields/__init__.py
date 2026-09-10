"""
Field arithmetic for :mod:`mathcore`.

``prime_field`` owns GF(p); ``binary_field`` (not yet built) will own GF(2^n).
Both are registered in ``config/math_registry.json`` under ``finite-fields``.
"""

from .prime_field import (
    CURVE25519_P,
    NIST_P256_P,
    SECP256K1_P,
    PrimeField,
    PseudoMersenne,
    Reduction,
    as_pseudo_mersenne,
)

__all__ = [
    "CURVE25519_P",
    "NIST_P256_P",
    "SECP256K1_P",
    "PrimeField",
    "PseudoMersenne",
    "Reduction",
    "as_pseudo_mersenne",
]
