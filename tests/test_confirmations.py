"""
Tests for :mod:`src.mathcore.probability.confirmations`.

The exit gate names two things: reproduce Nakamoto's section-11 table to four
decimals, and show `required_depth` is monotone in both value at risk and
attacker share -- as a property, not at sample points. Both are here.
"""

from __future__ import annotations

import pytest

from src.mathcore.probability.confirmations import (
    double_spend_probability,
    poisson_pmf,
    required_depth,
)

# Nakamoto, "Bitcoin", section 11, verbatim.
NAKAMOTO_Q10 = {
    0: 1.0,
    1: 0.2045873,
    2: 0.0509779,
    3: 0.0131722,
    4: 0.0034552,
    5: 0.0009137,
    6: 0.0002428,
    7: 0.0000647,
    8: 0.0000173,
    9: 0.0000046,
    10: 0.0000012,
}
NAKAMOTO_Q30 = {
    5: 0.1773523,
    10: 0.0416605,
    15: 0.0101008,
    20: 0.0024804,
    25: 0.0006132,
    30: 0.0001522,
    35: 0.0000379,
    40: 0.0000095,
    45: 0.0000024,
    50: 0.0000006,
}


# ---- the Nakamoto table (known answer) -------------------------------------


@pytest.mark.parametrize(("depth", "expected"), NAKAMOTO_Q10.items())
def test_reproduces_nakamoto_q10_to_four_decimals(depth: int, expected: float) -> None:
    assert double_spend_probability(0.1, depth) == pytest.approx(expected, abs=5e-8)


@pytest.mark.parametrize(("depth", "expected"), NAKAMOTO_Q30.items())
def test_reproduces_nakamoto_q30_to_four_decimals(depth: int, expected: float) -> None:
    assert double_spend_probability(0.3, depth) == pytest.approx(expected, abs=5e-8)


# ---- boundaries ------------------------------------------------------------


def test_zero_depth_is_certain_success() -> None:
    for q in (0.0, 0.1, 0.3, 0.49):
        assert double_spend_probability(q, 0) == 1.0


def test_a_majority_attacker_succeeds_at_any_depth() -> None:
    for depth in (1, 6, 100):
        assert double_spend_probability(0.5, depth) == 1.0
        assert double_spend_probability(0.7, depth) == 1.0


def test_a_zero_hashpower_attacker_never_catches_up() -> None:
    for depth in (1, 6, 100):
        assert double_spend_probability(0.0, depth) == 0.0


def test_probability_stays_within_the_unit_interval() -> None:
    for depth in range(0, 60):
        p = double_spend_probability(0.45, depth)
        assert 0.0 <= p <= 1.0


def test_probability_is_monotone_decreasing_in_depth() -> None:
    """
    Monotone to within double precision. The probability is a ``1 - sum`` of
    Poisson terms, so once the true value drops below ~1e-15 the result is
    floating-point cancellation noise, and comparing 5e-17 against 2e-16 tests
    the FPU, not the mathematics. The tolerance is set just above that floor.
    """
    for q in (0.1, 0.25, 0.4):
        values = [double_spend_probability(q, d) for d in range(0, 40)]
        assert all(a >= b - 1e-12 for a, b in zip(values, values[1:], strict=False))


@pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5])
def test_an_out_of_range_share_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="attacker_share"):
        double_spend_probability(bad, 6)


def test_a_negative_depth_is_refused() -> None:
    with pytest.raises(ValueError, match="depth"):
        double_spend_probability(0.1, -1)


# ---- Poisson pmf -----------------------------------------------------------


def test_poisson_pmf_matches_hand_values() -> None:
    import math

    assert poisson_pmf(0, 0.0) == 1.0
    assert poisson_pmf(1, 0.0) == 0.0
    assert poisson_pmf(0, 2.0) == pytest.approx(math.exp(-2))
    assert poisson_pmf(2, 3.0) == pytest.approx(math.exp(-3) * 9 / 2)


def test_poisson_pmf_sums_to_one_over_its_support() -> None:
    total = sum(poisson_pmf(k, 4.0) for k in range(0, 60))
    assert total == pytest.approx(1.0, abs=1e-9)


def test_poisson_pmf_handles_a_large_mean_without_overflow() -> None:
    """log-space computation: mean**k would overflow long before this."""
    assert 0.0 < poisson_pmf(1000, 1000.0) < 1.0


@pytest.mark.parametrize(("k", "mean"), [(-1, 1.0), (1, -1.0)])
def test_poisson_pmf_refuses_bad_arguments(k: int, mean: float) -> None:
    with pytest.raises(ValueError):
        poisson_pmf(k, mean)


# ---- required_depth --------------------------------------------------------


def test_required_depth_is_monotone_in_value_at_risk() -> None:
    """
    Property, not sample points: for a fixed attacker and tolerance, a larger
    payment never needs fewer confirmations.
    """
    depths = [
        required_depth(0.1, var, acceptable_loss=1.0)
        for var in (1.0, 10.0, 100.0, 1_000.0, 10_000.0, 1_000_000.0)
    ]
    assert all(a <= b for a, b in zip(depths, depths[1:], strict=False))


def test_required_depth_is_monotone_in_attacker_share() -> None:
    """A stronger attacker never needs fewer confirmations."""
    depths = [
        required_depth(q, value_at_risk=10_000.0, acceptable_loss=1.0)
        for q in (0.01, 0.05, 0.1, 0.2, 0.3, 0.4)
    ]
    assert all(a <= b for a, b in zip(depths, depths[1:], strict=False))


def test_a_payment_within_tolerance_needs_no_confirmations() -> None:
    assert required_depth(0.3, value_at_risk=1.0, acceptable_loss=1.0) == 0
    assert required_depth(0.3, value_at_risk=0.5, acceptable_loss=1.0) == 0


def test_required_depth_actually_clears_the_bound() -> None:
    q, var, tol = 0.2, 5_000.0, 2.0
    depth = required_depth(q, var, tol)
    assert double_spend_probability(q, depth) * var <= tol
    if depth > 0:
        assert double_spend_probability(q, depth - 1) * var > tol


def test_an_unsatisfiable_bound_raises_rather_than_capping_silently() -> None:
    """
    A near-50% attacker's success probability decays so slowly that no
    practical depth settles a large payment. Returning the cap would tell a
    caller the payment was safe when it is not, so it raises.
    """
    with pytest.raises(ValueError, match="too close to half"):
        required_depth(0.49, value_at_risk=1e9, acceptable_loss=1e-9, max_depth=50)


def test_required_depth_refuses_a_nonpositive_tolerance() -> None:
    with pytest.raises(ValueError, match="acceptable_loss"):
        required_depth(0.1, 100.0, 0.0)


def test_required_depth_refuses_a_negative_value() -> None:
    with pytest.raises(ValueError, match="value_at_risk"):
        required_depth(0.1, -1.0, 1.0)


def test_required_depth_refuses_a_negative_max_depth() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        required_depth(0.1, 100.0, 1.0, max_depth=-1)
