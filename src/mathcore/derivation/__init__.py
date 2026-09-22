"""
Deterministic derivation for :mod:`mathcore`.

Two modules that sit on opposite sides of the secret boundary, which is the
thing to keep straight before importing either:

``nonces`` owns ``rfc6979-deterministic-nonces``. Unlike the rest of
:mod:`mathcore`, whose inputs are public, this module derives a secret; read
its module docstring before using it in anything that signs.

``bip32`` owns ``bip32-hd-derivation`` and is the opposite case: watch-only by
construction. It derives extended *public* keys and refuses hardened
derivation, so it can run on a trading host without spending authority
existing in that process.
"""

from .bip32 import (
    HARDENED_OFFSET,
    Bip32Error,
    ExtendedPublicKey,
    derive_addresses,
    derive_child,
    derive_path,
    parse_extended_key,
    parse_path,
)
from .nonces import bits2int, bits2octets, generate_k, int2octets

__all__ = [
    "HARDENED_OFFSET",
    "Bip32Error",
    "ExtendedPublicKey",
    "bits2int",
    "bits2octets",
    "derive_addresses",
    "derive_child",
    "derive_path",
    "generate_k",
    "int2octets",
    "parse_extended_key",
    "parse_path",
]
