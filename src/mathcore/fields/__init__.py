"""
Field arithmetic for :mod:`mathcore`.

``prime_field`` owns GF(p); ``binary_field`` owns GF(2^n) -- together the two
components of ``finite-fields``. ``ntt`` owns the Number-Theoretic Transform in
the ML-KEM ring, a separate registry entry that happens to live in this package
because it is field arithmetic.
"""

from .binary_field import (
    AES_POLY,
    GCM_POLY,
    BinaryField,
)
from .ntt import (
    ML_KEM_N,
    ML_KEM_Q,
    ZETA,
    base_case_multiply,
    intt,
    negacyclic_convolution,
    ntt,
    ntt_multiply,
    validate_kem_parameters,
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
    "ML_KEM_N",
    "ML_KEM_Q",
    "NIST_P256_P",
    "PrimeField",
    "PseudoMersenne",
    "Reduction",
    "SECP256K1_P",
    "ZETA",
    "as_pseudo_mersenne",
    "base_case_multiply",
    "intt",
    "negacyclic_convolution",
    "ntt",
    "ntt_multiply",
    "validate_kem_parameters",
]
