"""
Deterministic derivation and threshold splitting for :mod:`mathcore`.

``nonces`` owns ``rfc6979-deterministic-nonces``. Unlike the rest of
:mod:`mathcore`, whose inputs are public, this package derives a secret; read
its module docstring before using it in anything that signs.
"""

from .nonces import bits2int, bits2octets, generate_k, int2octets
from .threshold import (
    group_public_key,
    lagrange_coefficients_at_zero,
    reconstruct_secret,
    split_signing_key,
)

__all__ = [
    "bits2int",
    "bits2octets",
    "generate_k",
    "group_public_key",
    "int2octets",
    "lagrange_coefficients_at_zero",
    "reconstruct_secret",
    "split_signing_key",
]
