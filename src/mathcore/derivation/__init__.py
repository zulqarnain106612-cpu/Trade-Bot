"""
Deterministic derivation for :mod:`mathcore`.

``nonces`` owns ``rfc6979-deterministic-nonces``. Unlike the rest of
:mod:`mathcore`, whose inputs are public, this package derives a secret; read
its module docstring before using it in anything that signs.
"""

from .nonces import bits2int, bits2octets, generate_k, int2octets

__all__ = [
    "bits2int",
    "bits2octets",
    "generate_k",
    "int2octets",
]
