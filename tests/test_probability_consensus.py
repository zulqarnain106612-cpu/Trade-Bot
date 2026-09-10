"""
Tests for the consensus-safety probability modules.

Committee bounds are checked against the exact hypergeometric law (the Chernoff
bound must dominate it) and for the monotonicities a designer relies on.
Selfish mining is pinned to Eyal & Sirer's published thresholds -- 1/3 at
gamma=0, falling as gamma rises -- which is the whole result the entry exists
to capture.
"""

from __future__ import annotations

import pytest

from src.mathcore.probability.committee import (
    chernoff_committee_bound,
    committee_failure_probability,
    min_committee_size,
)
from src.mathcore.probability.reorg import (
    honest_revenue_share,
    selfish_mining_revenue,
    selfish_mining_threshold,
)

# ---- committee sampling ----------------------------------------------------


def test_exact_tail_against_a_hand_computed_hypergeometric() -> None:
    # Population 10, 5 adversaries, committee 4, P(all 4 adversaries).
    # = C(5,4)C(5,0)/C(10,4) = 5/210.
    assert committee_failure_probability(10, 5, 4, 4) == pytest.approx(5 / 210)


def test_a_committee_certainly_contains_at_least_zero_adversaries() -> None:
    assert committee_failure_probability(100, 30, 10, 0) == pytest.approx(1.0)


def test_failure_probability_rises_with_adversary_count() -> None:
    probs = [committee_failure_probability(1000, adv, 50, 25) for adv in (100, 200, 300, 400, 500)]
    assert all(a <= b for a, b in zip(probs, probs[1:], strict=False))


def test_failure_probability_falls_as_the_threshold_rises() -> None:
    probs = [committee_failure_probability(1000, 300, 100, t) for t in range(30, 80, 10)]
    assert all(a >= b for a, b in zip(probs, probs[1:], strict=False))


def test_the_chernoff_bound_dominates_the_exact_tail() -> None:
    """A valid upper bound is never below the probability it bounds."""
    exact = committee_failure_probability(2000, 400, 100, 50)
    bound = chernoff_committee_bound(0.2, 100, 0.5)
    assert bound >= exact


def test_the_chernoff_bound_falls_exponentially_with_size() -> None:
    bounds = [chernoff_committee_bound(0.2, n, 0.5) for n in (10, 50, 100, 200)]
    assert all(a > b for a, b in zip(bounds, bounds[1:], strict=False))


def test_the_chernoff_bound_is_trivial_below_the_mean() -> None:
    """Bounding a fraction at or below the adversary mean gives 1, not nonsense."""
    assert chernoff_committee_bound(0.3, 100, 0.2) == 1.0
    assert chernoff_committee_bound(0.3, 100, 0.3) == 1.0


def test_min_committee_size_meets_the_target() -> None:
    size = min_committee_size(0.2, 0.5, 2**-40)
    assert chernoff_committee_bound(0.2, size, 0.5) <= 2**-40
    assert chernoff_committee_bound(0.2, size - 1, 0.5) > 2**-40


def test_min_committee_size_grows_for_a_stricter_target() -> None:
    assert min_committee_size(0.2, 0.5, 1e-6) <= min_committee_size(0.2, 0.5, 1e-12)


def test_committee_bounds_validate_their_arguments() -> None:
    with pytest.raises(ValueError, match="adversaries"):
        committee_failure_probability(10, 20, 5, 1)
    with pytest.raises(ValueError, match="committee_size"):
        committee_failure_probability(10, 5, 20, 1)
    with pytest.raises(ValueError, match="adversary_fraction"):
        chernoff_committee_bound(1.5, 10, 0.5)
    with pytest.raises(ValueError, match="exceed adversary_fraction"):
        min_committee_size(0.5, 0.3, 1e-9)


# ---- selfish mining --------------------------------------------------------


def test_the_gamma_zero_threshold_is_one_third() -> None:
    """Eyal & Sirer's headline result."""
    assert selfish_mining_threshold(0.0) == pytest.approx(1 / 3, abs=1e-6)


def test_the_threshold_falls_as_gamma_rises() -> None:
    thresholds = [selfish_mining_threshold(g) for g in (0.0, 0.25, 0.5, 0.75)]
    assert all(a > b for a, b in zip(thresholds, thresholds[1:], strict=False))


def test_a_perfectly_connected_attacker_needs_no_hash_rate() -> None:
    assert selfish_mining_threshold(1.0) == 0.0


def test_selfish_beats_honest_above_the_threshold() -> None:
    threshold = selfish_mining_threshold(0.0)
    assert selfish_mining_revenue(threshold + 0.05, 0.0) > honest_revenue_share(threshold + 0.05)
    assert selfish_mining_revenue(threshold - 0.05, 0.0) < honest_revenue_share(threshold - 0.05)


def test_revenue_matches_the_eyal_sirer_formula() -> None:
    # Spot value: alpha=0.4, gamma=0.5, computed from the closed form.
    alpha, gamma = 0.4, 0.5
    num = alpha * (1 - alpha) ** 2 * (4 * alpha + gamma * (1 - 2 * alpha)) - alpha**3
    den = 1 - alpha * (1 + (2 - alpha) * alpha)
    assert selfish_mining_revenue(alpha, gamma) == pytest.approx(num / den)


def test_zero_hashrate_earns_zero() -> None:
    assert selfish_mining_revenue(0.0, 0.5) == pytest.approx(0.0)


def test_selfish_mining_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="alpha"):
        selfish_mining_revenue(0.5, 0.5)
    with pytest.raises(ValueError, match="gamma"):
        selfish_mining_revenue(0.3, 1.5)
    with pytest.raises(ValueError, match="alpha"):
        honest_revenue_share(0.6)
    with pytest.raises(ValueError, match="gamma"):
        selfish_mining_threshold(-0.1)


def test_committee_extra_guards() -> None:
    with pytest.raises(ValueError, match="threshold must be non-negative"):
        committee_failure_probability(10, 5, 4, -1)
    with pytest.raises(ValueError, match="committee_size must be at least 1"):
        chernoff_committee_bound(0.2, 0, 0.5)
    with pytest.raises(ValueError, match="threshold_fraction"):
        chernoff_committee_bound(0.2, 10, 1.5)
    with pytest.raises(ValueError, match="target_failure"):
        min_committee_size(0.2, 0.5, 1.5)


def test_chernoff_handles_a_full_adversarial_threshold() -> None:
    """threshold_fraction == 1 uses the KL(1||p) limit rather than log(0)."""
    bound = chernoff_committee_bound(0.2, 10, 1.0)
    assert bound == pytest.approx(0.2**10)


def test_selfish_threshold_converges_even_without_early_stop() -> None:
    """With tolerance 0 the bisection runs its full budget and still returns
    the 1/3 crossing -- exercising the loop-exhaustion path."""
    assert selfish_mining_threshold(0.0, tolerance=0.0) == pytest.approx(1 / 3, abs=1e-6)
