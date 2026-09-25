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

``threshold`` owns ``threshold-signatures`` and straddles the line: its
commitments are public and publishable, its shares are secret. It is
deliberately **not** a signer -- read its docstring on the ROS attack before
reaching for these pieces to build one.
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
from .threshold import (
    SharingCommitment,
    ThresholdError,
    commit_to_polynomial,
    group_public_key_from_shares,
    lagrange_coefficient_at_zero,
    participant_public_key,
    recover_secret,
    refute_share,
    shares_agree_with_group_key,
    split_secret,
    verify_share,
    verify_sharing,
)

__all__ = [
    "HARDENED_OFFSET",
    "Bip32Error",
    "ExtendedPublicKey",
    "SharingCommitment",
    "ThresholdError",
    "bits2int",
    "bits2octets",
    "commit_to_polynomial",
    "derive_addresses",
    "derive_child",
    "derive_path",
    "generate_k",
    "group_public_key_from_shares",
    "int2octets",
    "lagrange_coefficient_at_zero",
    "parse_extended_key",
    "parse_path",
    "participant_public_key",
    "recover_secret",
    "refute_share",
    "shares_agree_with_group_key",
    "split_secret",
    "verify_share",
    "verify_sharing",
]
