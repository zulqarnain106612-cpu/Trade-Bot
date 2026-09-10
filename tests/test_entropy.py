"""
Tests for :mod:`src.mathcore.probability.entropy`.

The exit gate asks that RNG auditing report *min-entropy*, not average. The
tests pin the two apart: on a uniform source they coincide, and on a skewed one
min-entropy is strictly and importantly smaller -- the gap being exactly how
much reporting Shannon entropy would overstate a weak generator's security.
"""

from __future__ import annotations

import math
import random

import pytest

from src.mathcore.probability.entropy import (
    audit_rng,
    min_entropy,
    shannon_entropy,
)

# ---- known values ----------------------------------------------------------


def test_a_fair_coin_is_one_bit_either_way() -> None:
    assert shannon_entropy([1, 1]) == pytest.approx(1.0)
    assert min_entropy([1, 1]) == pytest.approx(1.0)


def test_a_uniform_byte_is_eight_bits_either_way() -> None:
    counts = [1] * 256
    assert shannon_entropy(counts) == pytest.approx(8.0)
    assert min_entropy(counts) == pytest.approx(8.0)


def test_a_certain_outcome_is_zero_entropy() -> None:
    assert shannon_entropy([10, 0, 0]) == 0.0
    assert min_entropy([10, 0, 0]) == 0.0


def test_base_can_be_changed() -> None:
    """Natural units (nats) for a fair coin: ln 2."""
    assert shannon_entropy([1, 1], base=math.e) == pytest.approx(math.log(2))


# ---- the distinction the audit turns on ------------------------------------


def test_min_entropy_never_exceeds_shannon_entropy() -> None:
    rng = random.Random("entropy-order")
    for _ in range(200):
        counts = [rng.randrange(0, 100) for _ in range(rng.randint(2, 10))]
        if sum(counts) == 0:
            continue
        assert min_entropy(counts) <= shannon_entropy(counts) + 1e-12


def test_a_skewed_source_has_far_less_min_entropy_than_shannon() -> None:
    """
    The whole reason min-entropy is the security figure: a source that is
    almost always one value has middling Shannon entropy and near-zero
    min-entropy, and only the latter reflects the attacker's first guess.
    """
    counts = [9990, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    assert shannon_entropy(counts) > 5 * min_entropy(counts)
    assert min_entropy(counts) < 0.01


# ---- audit_rng -------------------------------------------------------------


def test_audit_reports_min_entropy_first_and_names_the_ceiling() -> None:
    rng = random.Random("audit-uniform")
    report = audit_rng(lambda: rng.randrange(256), samples=50_000, outcomes=256)
    assert report["max_bits"] == 8.0
    # A uniform source approaches, but with finite samples does not reach, the
    # ceiling; it must be close and never above.
    assert report["min_entropy_bits"] <= report["max_bits"]
    assert report["min_entropy_bits"] > 6.5
    assert report["min_entropy_bits"] <= report["shannon_entropy_bits"] + 1e-9


def test_audit_catches_a_biased_generator() -> None:
    """
    A generator uniform almost always but stuck on 0 one time in five has high
    Shannon entropy and low min-entropy. The audit must surface the low one.
    """
    rng = random.Random("audit-biased")

    def biased() -> int:
        return 0 if rng.random() < 0.2 else rng.randrange(256)

    report = audit_rng(biased, samples=50_000, outcomes=256)
    assert report["min_entropy_bits"] < 2.5  # dominated by the 20% mass on 0
    assert report["shannon_entropy_bits"] > report["min_entropy_bits"] + 1.0


def test_audit_rejects_an_out_of_range_draw() -> None:
    """An out-of-range value is a generator defect, not a rare outcome."""
    with pytest.raises(ValueError, match="out-of-range|outside"):
        audit_rng(lambda: 999, samples=10, outcomes=256)


@pytest.mark.parametrize(("samples", "outcomes"), [(0, 4), (10, 0)])
def test_audit_validates_its_arguments(samples: int, outcomes: int) -> None:
    with pytest.raises(ValueError):
        audit_rng(lambda: 0, samples=samples, outcomes=outcomes)


# ---- validation ------------------------------------------------------------


def test_entropy_needs_an_observation() -> None:
    with pytest.raises(ValueError, match="observation"):
        shannon_entropy([0, 0])
    with pytest.raises(ValueError, match="observation"):
        min_entropy([])


@pytest.mark.parametrize("bad_base", [1.0, 0.5, 0.0])
def test_a_degenerate_base_is_refused(bad_base: float) -> None:
    with pytest.raises(ValueError, match="base"):
        shannon_entropy([1, 1], base=bad_base)
    with pytest.raises(ValueError, match="base"):
        min_entropy([1, 1], base=bad_base)
