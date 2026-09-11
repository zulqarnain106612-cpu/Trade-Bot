"""
Tests for :mod:`src.mathcore.derivation.threshold`.

The property that matters is the same one Shamir gives, now over the secp256k1
scalar field: any t of m participants reconstruct the signing key and any t-1
cannot, and every valid quorum yields the same group public key. The Lagrange
coefficients are checked to combine shares to the secret without reconstructing
it -- the way a real threshold signer avoids ever holding the key.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.curves.secp256k1 import CURVE_ORDER
from src.mathcore.derivation.threshold import (
    SCALAR_FIELD,
    group_public_key,
    lagrange_coefficients_at_zero,
    reconstruct_secret,
    split_signing_key,
)


def _split(key: int, threshold: int, ids: list[int], seed: str):
    rng = random.Random(seed)
    coeffs = [rng.randrange(CURVE_ORDER) for _ in range(threshold - 1)]
    return split_signing_key(key, threshold, coeffs, ids)


# ---- threshold reconstruction ----------------------------------------------


@pytest.mark.parametrize("threshold", [1, 2, 3, 5])
def test_any_quorum_reconstructs_the_key(threshold: int) -> None:
    rng = random.Random(f"k{threshold}")
    key = rng.randrange(1, CURVE_ORDER)
    ids = list(range(1, threshold + 3))
    shares = _split(key, threshold, ids, f"s{threshold}")
    for start in range(len(shares) - threshold + 1):
        assert reconstruct_secret(shares[start : start + threshold]) == key


def test_a_sub_quorum_does_not_recover_the_key() -> None:
    key = 0xC0FFEE1234567
    shares = _split(key, 4, [1, 2, 3, 4, 5, 6], "sub")
    assert reconstruct_secret(shares[:3]) != key


def test_every_quorum_yields_the_same_group_public_key() -> None:
    """The outside world sees one key though no machine holds it."""
    rng = random.Random("group")
    key = rng.randrange(1, CURVE_ORDER)
    expected = group_public_key(key)
    shares = _split(key, 3, [10, 20, 30, 40, 50], "grp")
    for quorum in ([0, 1, 2], [1, 3, 4], [0, 2, 4]):
        recovered = reconstruct_secret([shares[i] for i in quorum])
        assert group_public_key(recovered) == expected


# ---- Lagrange combination without reconstruction ---------------------------


def test_lagrange_coefficients_combine_shares_to_the_secret() -> None:
    """
    sum_i coeff_i * share_i == secret -- the identity a threshold signer uses to
    combine partial signatures without assembling the key on one machine.
    """
    key = 0xABCDEF0123456789
    shares = _split(key, 3, [7, 11, 13, 17], "combine")
    quorum = shares[:3]
    xs = [x for x, _ in quorum]
    ys = [y for _, y in quorum]
    coeffs = lagrange_coefficients_at_zero(xs)
    combined = 0
    for c, y in zip(coeffs, ys, strict=True):
        combined = SCALAR_FIELD.add(combined, SCALAR_FIELD.mul(c, y))
    assert combined == key


def test_the_coefficients_depend_only_on_the_participant_set() -> None:
    first = lagrange_coefficients_at_zero([1, 2, 3])
    second = lagrange_coefficients_at_zero([1, 2, 3])
    assert first == second


def test_coefficients_reject_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="distinct"):
        lagrange_coefficients_at_zero([1, 1, 2])


def test_coefficients_reject_a_zero_id() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        lagrange_coefficients_at_zero([0, 1, 2])


# ---- validation ------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, CURVE_ORDER, CURVE_ORDER + 1])
def test_splitting_rejects_a_non_scalar_key(bad: int) -> None:
    with pytest.raises(ValueError, match=r"\[1, n\)"):
        split_signing_key(bad, 2, [5], [1, 2])


def test_group_public_key_rejects_a_non_scalar_key() -> None:
    with pytest.raises(ValueError, match=r"\[1, n\)"):
        group_public_key(0)


def test_split_delegates_shamir_validation() -> None:
    """A bad coefficient count is caught by the underlying Shamir split."""
    with pytest.raises(ValueError, match="coefficients"):
        split_signing_key(42, 3, [1], [1, 2, 3])
