"""
Field arithmetic for :mod:`mathcore`.

``prime_field`` owns GF(p); ``binary_field`` owns GF(2^n). Both are registered
in ``config/math_registry.json`` under ``finite-fields``, which is the one
entry naming two owning modules -- see that entry's note.
"""

from .binary_field import (
    AES_POLY,
    GCM_POLY,
    BinaryField,
)
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
    "AES_POLY",
    "BinaryField",
    "CURVE25519_P",
    "GCM_POLY",
    "NIST_P256_P",
    "PrimeField",
    "PseudoMersenne",
    "Reduction",
    "SECP256K1_P",
    "as_pseudo_mersenne",
]
