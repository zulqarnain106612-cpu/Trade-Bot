"""
Elliptic curves for :mod:`mathcore`.

``secp256k1`` owns ``elliptic-curves`` and ``cyclic-groups-dlp``. ``ed25519``,
the remaining phase-2 module in ``docs/MATH_ROADMAP.md``, is not built yet; the
registry, not this file list, is the record of what exists.
"""

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
    "is_on_curve",
    "parse_point",
    "scalar_multiply",
    "serialize_point",
    "validate_public_key",
    "verify_ecdsa",
]
