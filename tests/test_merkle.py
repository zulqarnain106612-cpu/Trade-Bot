"""
Tests for :mod:`src.mathcore.commitments.merkle`.

The exit gate: merkle_root is domain-separated by default, and a test
demonstrates the second-preimage collision the undefended version admits. Both
are here -- the collision is constructed, not asserted abstractly -- alongside
proof round-trips across every tree size, since the odd-node promotion is where
a Merkle implementation usually goes wrong.
"""

from __future__ import annotations

import hashlib

import pytest

from src.mathcore.commitments.merkle import (
    merkle_proof,
    merkle_root,
    verify_proof,
)


def _leaves(n: int) -> list[bytes]:
    return [f"leaf-{i}".encode() for i in range(n)]


# ---- proofs round-trip at every size ---------------------------------------


@pytest.mark.parametrize("n", range(1, 34))
def test_every_leaf_has_a_verifying_proof(n: int) -> None:
    """Odd sizes exercise node promotion, the usual source of Merkle bugs."""
    leaves = _leaves(n)
    root = merkle_root(leaves)
    for i in range(n):
        assert verify_proof(leaves[i], merkle_proof(leaves, i), root)


def test_a_proof_does_not_verify_against_the_wrong_leaf() -> None:
    leaves = _leaves(8)
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 3)
    assert verify_proof(leaves[3], proof, root)
    assert not verify_proof(b"not-the-leaf", proof, root)


def test_a_proof_does_not_verify_against_the_wrong_root() -> None:
    leaves = _leaves(8)
    other_root = merkle_root(_leaves(8)[:-1] + [b"tampered"])
    assert not verify_proof(leaves[0], merkle_proof(leaves, 0), other_root)


def test_a_single_leaf_tree_root_is_the_leaf_hash() -> None:
    leaf = b"only"
    root = merkle_root([leaf])
    assert verify_proof(leaf, merkle_proof([leaf], 0), root)
    assert merkle_proof([leaf], 0).siblings == ()


# ---- domain separation and the second-preimage attack ----------------------


def test_domain_separation_is_the_default() -> None:
    leaves = _leaves(4)
    assert merkle_root(leaves) != merkle_root(leaves, domain_separated=False)


def test_the_undefended_tree_admits_a_second_preimage() -> None:
    """
    The attack the default prevents, constructed end to end. In the undefended
    scheme a leaf is hashed as H(leaf) and an internal node as H(left||right),
    so the concatenation of two leaf hashes is itself a valid leaf whose hash
    equals their parent node. Two different leaf lists, one root.
    """
    leaves = [b"a", b"b", b"c", b"d"]
    root = merkle_root(leaves, domain_separated=False)

    ha = hashlib.sha256(b"a").digest()
    hb = hashlib.sha256(b"b").digest()
    hc = hashlib.sha256(b"c").digest()
    hd = hashlib.sha256(b"d").digest()

    # A forged two-leaf list: the first "leaf" is the concatenation ha||hb,
    # whose undefended leaf hash H(ha||hb) equals the real tree's ab node; the
    # second is hc||hd likewise. Its root collides with the four-leaf root.
    forged = [ha + hb, hc + hd]
    forged_root = merkle_root(forged, domain_separated=False)
    assert forged_root == root
    assert forged != leaves  # genuinely different commitments, same root


def test_domain_separation_defeats_that_collision() -> None:
    """The same construction against the default tree does not collide."""
    leaves = [b"a", b"b", b"c", b"d"]
    root = merkle_root(leaves)  # domain-separated

    ha = hashlib.sha256(b"\x00a").digest()
    hb = hashlib.sha256(b"\x00b").digest()
    hc = hashlib.sha256(b"\x00c").digest()
    hd = hashlib.sha256(b"\x00d").digest()
    forged = [ha + hb, hc + hd]
    assert merkle_root(forged) != root


def test_a_domain_separated_proof_fails_against_an_undefended_root() -> None:
    """The proof carries its rule; it cannot be checked under the other one."""
    leaves = _leaves(4)
    ds_root = merkle_root(leaves)
    undefended_proof = merkle_proof(leaves, 0, domain_separated=False)
    assert not verify_proof(leaves[0], undefended_proof, ds_root)


# ---- validation ------------------------------------------------------------


def test_an_empty_tree_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one leaf"):
        merkle_root([])


def test_an_out_of_range_proof_index_is_refused() -> None:
    with pytest.raises(ValueError, match="out of range"):
        merkle_proof(_leaves(4), 4)
    with pytest.raises(ValueError, match="out of range"):
        merkle_proof(_leaves(4), -1)


def test_the_root_is_stable_across_calls() -> None:
    leaves = _leaves(7)
    assert merkle_root(leaves) == merkle_root(leaves)
