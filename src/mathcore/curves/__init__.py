"""
Elliptic curves for :mod:`mathcore`.

``secp256k1`` owns ``elliptic-curves`` and ``cyclic-groups-dlp``; ``ed25519``
owns ``edwards-curves``; ``bls12_381`` owns ``bilinear-pairings``. Both export a ``Point``, and they are different types
over different curves -- import them qualified rather than from here when both
are in play.
"""

from . import bls12_381, ed25519, secp256k1
from .secp256k1 import (
    CURVE_ORDER,
    FIELD_PRIME,
    GENERATOR,
    INFINITY,
    Point,
    is_on_curve,
    parse_point,
    scalar_multiply,
    serialize_point,
    validate_public_key,
    verify_ecdsa,
)

__all__ = [
    "CURVE_ORDER",
    "FIELD_PRIME",
    "GENERATOR",
    "INFINITY",
    "Point",
    "bls12_381",
    "ed25519",
    "is_on_curve",
    "parse_point",
    "scalar_multiply",
    "secp256k1",
    "serialize_point",
    "validate_public_key",
    "verify_ecdsa",
]
