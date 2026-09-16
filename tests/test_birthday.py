"""
Tests for :mod:`src.mathcore.probability.birthday`.

The n=365 birthday problem is the known-answer oracle: 23 people for a better-
than-even chance, and 0.5073 at exactly 23. The cryptographic point -- that a
b-bit space collides after about 2**(b/2) draws -- is checked against the
sqrt law, and the exact/approximate split inside the module is checked to agree
where they overlap.
"""

from __future__ import annotations

import math

import pytest

from src.mathcore.probability.birthday import (
    birthday_bound,
    birthday_collision_probability,
)

# ---- the classic birthday problem (known answer) ---------------------------


def test_the_year_needs_23_people_for_even_odds() -> None:
    assert birthday_bound(365) == 23


def test_the_probability_at_23_matches_the_textbook_value() -> None:
    assert birthday_collision_probability(23, 365) == pytest.approx(0.5073, abs=1e-4)
    assert birthday_collision_probability(22, 365) < 0.5
    assert birthday_collision_probability(23, 365) >= 0.5


def test_probability_grows_monotonically_with_draws() -> None:
    values = [birthday_collision_probability(k, 365) for k in range(0, 100)]
    assert all(a <= b for a, b in zip(values, values[1:], strict=False))


# ---- boundaries ------------------------------------------------------------


def test_fewer_than_two_draws_cannot_collide() -> None:
    assert birthday_collision_probability(0, 1000) == 0.0
    assert birthday_collision_probability(1, 1000) == 0.0


def test_more_draws_than_the_space_collide_with_certainty() -> None:
    """Pigeonhole: more pigeons than holes forces a repeat."""
    assert birthday_collision_probability(1001, 1000) == 1.0
    assert birthday_collision_probability(2, 1) == 1.0


def test_probability_stays_within_the_unit_interval() -> None:
    for k in range(0, 400):
        assert 0.0 <= birthday_collision_probability(k, 365) <= 1.0


# ---- the cryptographic sqrt law --------------------------------------------


@pytest.mark.parametrize("bits", [32, 48, 64, 80, 128])
def test_the_collision_bound_tracks_the_square_root_of_the_space(bits: int) -> None:
    """
    A b-bit space collides at even odds after ~1.1774 * 2**(b/2) draws. The
    reason 128-bit hashes give 64-bit collision resistance, checked to a few
    percent against the analytic constant.
    """
    space = 1 << bits
    bound = birthday_bound(space)
    expected = 1.1774 * math.sqrt(space)
    assert bound == pytest.approx(expected, rel=0.01)


def test_the_exact_and_approximate_branches_agree_at_the_boundary() -> None:
    """
    The module computes small draw counts by exact product and large ones by
    the closed form. At a space size straddling the cutoff the two must give
    the same answer to well within any decision made from it.
    """
    from src.mathcore.probability.birthday import _EXACT_PRODUCT_LIMIT

    space = 1 << 60
    k = _EXACT_PRODUCT_LIMIT
    exact = birthday_collision_probability(k, space)
    approx = -math.expm1(-k * (k - 1) / (2.0 * space))
    assert exact == pytest.approx(approx, rel=1e-6)


def test_a_large_space_bound_matches_the_analytic_value() -> None:
    """2**64 collides at even odds near 5.06e9 -- computed without looping
    billions of times, via the closed-form branch."""
    bound = birthday_bound(2**64)
    assert bound == pytest.approx(1.1774 * 2**32, rel=0.01)


# ---- thresholds and validation ---------------------------------------------


def test_a_higher_threshold_needs_more_draws() -> None:
    assert birthday_bound(365, 0.5) <= birthday_bound(365, 0.9)
    assert birthday_bound(365, 0.9) <= birthday_bound(365, 0.99)


def test_the_returned_count_actually_crosses_the_threshold() -> None:
    for threshold in (0.1, 0.5, 0.9, 0.99):
        k = birthday_bound(10_000, threshold)
        assert birthday_collision_probability(k, 10_000) >= threshold
        if k > 0:
            assert birthday_collision_probability(k - 1, 10_000) < threshold


@pytest.mark.parametrize("bad", [0, -5])
def test_a_degenerate_space_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="space_size"):
        birthday_collision_probability(2, bad)
    with pytest.raises(ValueError, match="space_size"):
        birthday_bound(bad)


def test_a_negative_draw_count_is_refused() -> None:
    with pytest.raises(ValueError, match="draws"):
        birthday_collision_probability(-1, 100)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.1])
def test_a_threshold_outside_the_unit_interval_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        birthday_bound(365, bad)


def test_a_threshold_of_one_is_the_whole_space() -> None:
    """Certainty needs every hole filled but one -- the pigeonhole count."""
    assert birthday_bound(10, 1.0) == 11
