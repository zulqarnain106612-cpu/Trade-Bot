"""
Merkle trees, domain-separated by default.

Owns the ``merkle-commitments`` registry entry.

A Merkle tree commits to a list of leaves in a single root hash, and lets a
holder prove one leaf is in the list with a path of ``log n`` hashes. The
subtlety that has bitten real systems (Bitcoin's CVE-2012-2459 among them) is
the *second-preimage* attack: if a leaf and an internal node are hashed the
same way, an attacker can pass off an internal node's two children,
concatenated, as a single leaf -- because ``H(left || right)`` is both the
internal node and a valid leaf hash of that concatenation. The tree then has
two different leaf lists with the same root.

The fix is domain separation: prefix a leaf hash with one byte and an internal
hash with another, so ``H(0x00 || leaf)`` can never equal ``H(0x01 || l || r)``.
This module does that by default and offers the undefended mode only so a test
can demonstrate the very collision the default prevents.

Hashing is SHA-256. Everything is public commitment data, so constant-time does
not apply.

References: Merkle (1988); RFC 6962 (Certificate Transparency, which
domain-separates for exactly this reason); Bitcoin CVE-2012-2459.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "LEAF_PREFIX",
    "NODE_PREFIX",
    "MerkleProof",
    "merkle_proof",
    "merkle_root",
    "verify_proof",
]

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def _leaf_hash(data: bytes, domain_separated: bool) -> bytes:
    return hashlib.sha256((LEAF_PREFIX if domain_separated else b"") + data).digest()


def _node_hash(left: bytes, right: bytes, domain_separated: bool) -> bytes:
    prefix = NODE_PREFIX if domain_separated else b""
    return hashlib.sha256(prefix + left + right).digest()


@dataclass(frozen=True)
class MerkleProof:
    """
    An inclusion proof for one leaf.

    Each step in ``siblings`` is ``(sibling_hash, current_is_left)``: the hash
    to combine with, and whether the running hash sits on the left of that
    combination. The direction is recorded explicitly rather than derived from
    ``index`` bits, because odd-level node promotion skips levels and desyncs
    any index-parity scheme. ``domain_separated`` records which hashing rule
    built the proof, so :func:`verify_proof` cannot be tricked into checking a
    domain-separated proof under the undefended rule or vice versa.
    """

    index: int
    siblings: tuple[tuple[bytes, bool], ...]
    domain_separated: bool


def merkle_root(leaves: Sequence[bytes], *, domain_separated: bool = True) -> bytes:
    """
    The Merkle root of ``leaves``.

    Domain-separated by default; pass ``domain_separated=False`` only to study
    the second-preimage weakness that choice removes. An odd level promotes its
    last node unpaired (the common convention) rather than duplicating it, which
    is itself a source of malleability when duplication is used instead.

    An empty leaf list has no commitment and is refused rather than assigned a
    conventional zero root, which would make two different "nothing"s compare
    equal. Not constant time.
    """
    if not leaves:
        raise ValueError("a Merkle tree needs at least one leaf")
    level = [_leaf_hash(leaf, domain_separated) for leaf in leaves]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(_node_hash(level[i], level[i + 1], domain_separated))
        if len(level) % 2 == 1:
            nxt.append(level[-1])  # promote the odd node unpaired
        level = nxt
    return level[0]


def merkle_proof(
    leaves: Sequence[bytes], index: int, *, domain_separated: bool = True
) -> MerkleProof:
    """
    An inclusion proof for ``leaves[index]``.

    Rebuilds the tree recording the sibling at each level. The odd-node
    promotion matches :func:`merkle_root`: a promoted node has no sibling at
    that level, so the path simply skips it. Not constant time.
    """
    if not 0 <= index < len(leaves):
        raise ValueError(f"index {index} out of range for {len(leaves)} leaves")
    level = [_leaf_hash(leaf, domain_separated) for leaf in leaves]
    position = index
    siblings: list[tuple[bytes, bool]] = []
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(_node_hash(level[i], level[i + 1], domain_separated))
        if len(level) % 2 == 1:
            nxt.append(level[-1])
        if position == len(level) - 1 and len(level) % 2 == 1:
            # promoted unpaired: no sibling at this level, rises as-is
            position //= 2
        else:
            current_is_left = position % 2 == 0
            siblings.append((level[position ^ 1], current_is_left))
            position //= 2
        level = nxt
    return MerkleProof(index=index, siblings=tuple(siblings), domain_separated=domain_separated)


def verify_proof(leaf: bytes, proof: MerkleProof, root: bytes) -> bool:
    """
    Whether ``proof`` shows ``leaf`` is committed by ``root``.

    Recomputes the root from the leaf and the sibling path under the proof's own
    domain-separation rule, and compares. A proof built without domain
    separation will not verify against a domain-separated root, which is the
    property that closes the second-preimage hole. Not constant time.
    """
    current = _leaf_hash(leaf, proof.domain_separated)
    for sibling, current_is_left in proof.siblings:
        if current_is_left:
            current = _node_hash(current, sibling, proof.domain_separated)
        else:
            current = _node_hash(sibling, current, proof.domain_separated)
    return current == root
