"""
Committee-sampling safety bounds.

Owns the ``chernoff-committee-bounds`` registry entry.

Sampled-committee consensus -- Algorand, committee-based BFT, sharded chains --
rests on one probability: that a small committee drawn from a large validator
set is very unlikely to contain enough adversaries to control it. This module
computes that probability exactly and bounds it, so a committee size can be
chosen for a target failure rate rather than by guesswork.

Two views:

* the **exact** tail, by the hypergeometric distribution -- committees are
  sampled *without replacement* from a finite set, so this, not the binomial,
  is the true law;
* the **Chernoff** upper bound, the closed form that is loose but lets a
  designer solve for a committee size analytically.

Both are pure rational/float arithmetic on public parameters; constant-time
does not apply.

References: Chen & Micali, *Algorand* (2016); Hoeffding (1963) on sampling
without replacement; Mitzenmacher & Upfal, *Probability and Computing*.
"""

from __future__ import annotations

import math

__all__ = [
    "chernoff_committee_bound",
    "committee_failure_probability",
    "min_committee_size",
]


def committee_failure_probability(
    population: int, adversaries: int, committee_size: int, threshold: int
) -> float:
    """
    The exact probability a sampled committee contains at least ``threshold``
    adversaries.

    The upper tail of the hypergeometric distribution: draw ``committee_size``
    members without replacement from ``population`` of whom ``adversaries`` are
    corrupt, and sum ``P(X = k)`` for ``k >= threshold``. This is the failure
    probability a committee scheme must drive below its security target.

    Preconditions are checked -- the sample cannot exceed the population, the
    adversary count cannot exceed it -- because a silently clamped parameter
    would report a safety level for a scenario the caller did not describe.
    Not constant time.
    """
    if not 0 <= adversaries <= population:
        raise ValueError("adversaries must be in [0, population]")
    if not 0 <= committee_size <= population:
        raise ValueError("committee_size must be in [0, population]")
    if threshold < 0:
        raise ValueError(f"threshold must be non-negative, got {threshold}")

    total = math.comb(population, committee_size)
    honest = population - adversaries
    probability = 0.0
    lowest = max(threshold, committee_size - honest)
    highest = min(committee_size, adversaries)
    for k in range(lowest, highest + 1):
        # Divide each term by the total as it is formed. The combinatorial
        # numerators are exact big integers; only the per-term ratio (at most 1)
        # is taken to float, so a large population cannot overflow the running
        # sum the way accumulating giant integers first would.
        term = math.comb(adversaries, k) * math.comb(honest, committee_size - k)
        probability += term / total
    return probability


def chernoff_committee_bound(
    adversary_fraction: float, committee_size: int, threshold_fraction: float
) -> float:
    """
    A Chernoff upper bound on the committee-failure probability.

    For a committee of ``committee_size`` drawn from a population with adversary
    fraction ``p = adversary_fraction``, bounds ``P(X >= a * committee_size)``
    for a target fraction ``a = threshold_fraction > p`` by the Chernoff form
    ``exp(-n * KL(a || p))`` with ``KL`` the binary relative entropy. Looser
    than :func:`committee_failure_probability` but closed-form, so a designer
    can invert it for a committee size.

    Requires ``a > p`` -- the bound is on the upper tail of an event more
    adversarial than the mean, and is trivially 1 otherwise, which is returned
    rather than a meaningless number. Not constant time.
    """
    if not 0 < adversary_fraction < 1:
        raise ValueError(f"adversary_fraction must be in (0, 1), got {adversary_fraction}")
    if committee_size < 1:
        raise ValueError(f"committee_size must be at least 1, got {committee_size}")
    if not 0 < threshold_fraction <= 1:
        raise ValueError(f"threshold_fraction must be in (0, 1], got {threshold_fraction}")
    if threshold_fraction <= adversary_fraction:
        return 1.0

    a, p = threshold_fraction, adversary_fraction
    if a >= 1.0:
        kl = math.log(1 / p)  # KL(1 || p) limit
    else:
        kl = a * math.log(a / p) + (1 - a) * math.log((1 - a) / (1 - p))
    return math.exp(-committee_size * kl)


def min_committee_size(
    adversary_fraction: float,
    threshold_fraction: float,
    target_failure: float,
) -> int:
    """
    The smallest committee whose Chernoff bound is at or below ``target_failure``.

    Inverts :func:`chernoff_committee_bound`: since the bound falls
    exponentially in the size, ``ceil(-ln(target) / KL(a || p))`` is the size,
    confirmed against the bound to absorb rounding. This is the number a
    designer actually wants -- "how many members for a 2^-40 failure rate".
    Not constant time.
    """
    if not 0 < target_failure < 1:
        raise ValueError(f"target_failure must be in (0, 1), got {target_failure}")
    if threshold_fraction <= adversary_fraction:
        raise ValueError(
            "threshold_fraction must exceed adversary_fraction; otherwise no "
            "finite committee makes the failure rare"
        )
    size = 1
    while chernoff_committee_bound(adversary_fraction, size, threshold_fraction) > target_failure:
        size += 1
    return size
