"""
Commitment schemes for :mod:`mathcore`.

``merkle`` owns ``merkle-trees``: domain-separated Merkle commitments and
inclusion proofs, with the undefended mode kept only to demonstrate the
second-preimage attack the default prevents.
"""

from .merkle import (
    LEAF_PREFIX,
    NODE_PREFIX,
    MerkleProof,
    merkle_proof,
    merkle_root,
    verify_proof,
)

__all__ = [
    "LEAF_PREFIX",
    "NODE_PREFIX",
    "MerkleProof",
    "merkle_proof",
    "merkle_root",
    "verify_proof",
]
