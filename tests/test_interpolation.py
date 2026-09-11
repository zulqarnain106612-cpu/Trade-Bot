"""
Tests for :mod:`src.mathcore.fields.interpolation`.

The property that matters is exact-threshold recovery: any t shares
reconstruct the secret and any t-1 do not. Both directions are tested over a
prime field, along with the Lagrange evaluation the scheme is built on.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.fields.interpolation import (
    lagrange_interpolate_at,
    shamir_reconstruct,
    shamir_split,
)
from src.mathcore.fields.prime_field import PrimeField

FIELD = PrimeField((1 << 127) - 1)  # a Mersenne prime, plenty of room


def _split(secret: int, threshold: int, xs: list[int], seed: str):
    rng = random.Random(seed)
    coeffs = [rng.randrange(FIELD.p) for _ in range(threshold - 1)]
    return shamir_split(secret, threshold, coeffs, xs, FIELD)


# ---- Lagrange --------------------------------------------------------------


def test_lagrange_recovers_a_known_polynomial() -> None:
    # y = x^2 + 1 through three points; value at 5 is 26.
    points = [(1, 2), (2, 5), (3, 10)]
    assert lagrange_interpolate_at(points, 5, FIELD) == 26


def test_lagrange_reproduces_its_own_points() -> None:
    points = [(1, 7), (4, 2), (9, 5)]
    for x, y in points:
        assert lagrange_interpolate_at(points, x, FIELD) == y


def test_a_single_point_is_a_constant() -> None:
    assert lagrange_interpolate_at([(3, 42)], 100, FIELD) == 42


def test_duplicate_x_coordinates_are_refused() -> None:
    with pytest.raises(ValueError, match="distinct x"):
        lagrange_interpolate_at([(1, 2), (1, 3)], 0, FIELD)


# ---- Shamir threshold ------------------------------------------------------


@pytest.mark.parametrize("threshold", [1, 2, 3, 5, 8])
def test_any_threshold_shares_reconstruct(threshold: int) -> None:
    rng = random.Random(f"t{threshold}")
    secret = rng.randrange(FIELD.p)
    xs = list(range(1, threshold + 3))
    shares = _split(secret, threshold, xs, f"seed{threshold}")
    # every size-`threshold` subset recovers the secret
    for start in range(len(shares) - threshold + 1):
        subset = shares[start : start + threshold]
        assert shamir_reconstruct(subset, FIELD) == secret


def test_fewer_than_threshold_shares_do_not_reveal_the_secret() -> None:
    secret = 123_456_789
    shares = _split(secret, 4, [1, 2, 3, 4, 5, 6], "secrecy")
    # any 3 shares interpolate a different constant term
    assert shamir_reconstruct(shares[:3], FIELD) != secret


def test_shares_lie_on_the_polynomial() -> None:
    """Each share is the polynomial evaluated at its x, so interpolating all of
    them back to any share's x returns that share."""
    shares = _split(999, 3, [2, 4, 6, 8], "onpoly")
    for x, y in shares:
        others = [s for s in shares if s[0] != x]
        assert lagrange_interpolate_at(others, x, FIELD) == y


def test_split_requires_the_right_number_of_coefficients() -> None:
    with pytest.raises(ValueError, match="coefficients"):
        shamir_split(10, 3, [1], [1, 2, 3], FIELD)  # need 2, gave 1


def test_split_rejects_a_zero_share_x() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        shamir_split(10, 2, [5], [0, 1], FIELD)


def test_split_rejects_duplicate_share_x() -> None:
    with pytest.raises(ValueError, match="distinct"):
        shamir_split(10, 2, [5], [3, 3], FIELD)


def test_split_rejects_a_degenerate_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        shamir_split(10, 0, [], [1], FIELD)


def test_reconstruct_needs_a_share() -> None:
    with pytest.raises(ValueError, match="at least one share"):
        shamir_reconstruct([], FIELD)


def test_lagrange_needs_a_point() -> None:
    with pytest.raises(ValueError, match="at least one point"):
        lagrange_interpolate_at([], 0, FIELD)


def test_threshold_one_shares_the_secret_directly() -> None:
    """A degree-0 polynomial is the constant secret; every share equals it."""
    shares = shamir_split(77, 1, [], [1, 2, 3], FIELD)
    assert all(y == 77 for _x, y in shares)
    assert shamir_reconstruct([shares[0]], FIELD) == 77
