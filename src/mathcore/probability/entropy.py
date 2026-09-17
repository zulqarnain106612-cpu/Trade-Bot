"""
Entropy measures for auditing randomness.

Owns the ``shannon-entropy`` registry entry.

Shannon entropy is the average surprise of a distribution, and it is the wrong
number for a security audit -- it can be high while the single most likely
outcome is nearly certain. The number that matters for a key or a nonce is
**min-entropy**, the surprise of the *most likely* outcome, because an attacker
guesses that one first. This module offers both and names the distinction
loudly, because reporting Shannon entropy as "the entropy" of an RNG is a
classic way to certify a broken generator as sound.

:func:`audit_rng` samples a generator and reports its min-entropy per output,
which is the figure the roadmap requires -- not the average.

References: Shannon (1948); NIST SP 800-90B (min-entropy as the security
measure); RFC 4086.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence

__all__ = [
    "audit_rng",
    "min_entropy",
    "shannon_entropy",
]


def _probabilities(counts: Sequence[int]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        raise ValueError("need at least one observation to measure entropy")
    return [c / total for c in counts if c > 0]


def shannon_entropy(counts: Sequence[int], *, base: float = 2.0) -> float:
    """
    Shannon entropy ``-sum p log p`` of an empirical distribution, in bits.

    ``counts`` is the observed frequency of each outcome; zero-count outcomes
    contribute nothing and are skipped. Returns 0.0 for a single observed
    outcome (no uncertainty).

    This is the *average* surprise. For an RNG audit it is the wrong measure --
    see :func:`min_entropy` and :func:`audit_rng` -- and it is offered mainly so
    the two can be compared and the gap made visible. Not constant time.
    """
    if base <= 1:
        raise ValueError(f"base must be greater than 1, got {base}")
    probabilities = _probabilities(counts)
    return -sum(p * math.log(p, base) for p in probabilities)


def min_entropy(counts: Sequence[int], *, base: float = 2.0) -> float:
    """
    Min-entropy ``-log(max p)`` of an empirical distribution, in bits.

    The surprise of the single most likely outcome, and the only entropy figure
    that bounds an attacker's best guessing strategy: it is never larger than
    Shannon entropy, and the gap is exactly how much a naive average overstates
    the security of a skewed source. Not constant time.
    """
    if base <= 1:
        raise ValueError(f"base must be greater than 1, got {base}")
    probabilities = _probabilities(counts)
    return -math.log(max(probabilities), base)


def audit_rng(
    generator: Callable[[], int],
    *,
    samples: int = 100_000,
    outcomes: int,
) -> dict[str, float]:
    """
    Sample ``generator`` and report its entropy, min-entropy first.

    ``generator`` returns an integer in ``[0, outcomes)`` per call; it is
    sampled ``samples`` times and the empirical distribution measured. The
    returned dict leads with ``min_entropy_bits`` -- the figure that matters --
    alongside ``shannon_entropy_bits`` and ``max_bits`` (``log2(outcomes)``, the
    entropy of a perfect uniform source) so a caller can see both the security
    figure and how far the source falls short of ideal.

    Reports min-entropy, not average, deliberately: a generator that is uniform
    almost always but returns 0 one time in a thousand has near-perfect Shannon
    entropy and catastrophic min-entropy, and only the latter reflects what an
    attacker exploits. An out-of-range draw is a generator bug and is raised
    rather than folded into the distribution. Not constant time.
    """
    if samples < 1:
        raise ValueError(f"samples must be at least 1, got {samples}")
    if outcomes < 1:
        raise ValueError(f"outcomes must be at least 1, got {outcomes}")

    counter: Counter[int] = Counter()
    for _ in range(samples):
        value = generator()
        if not 0 <= value < outcomes:
            raise ValueError(
                f"generator returned {value}, outside [0, {outcomes}); an "
                "out-of-range draw is a generator defect, not a rare outcome"
            )
        counter[value] += 1

    counts = [counter.get(i, 0) for i in range(outcomes)]
    return {
        "min_entropy_bits": min_entropy(counts),
        "shannon_entropy_bits": shannon_entropy(counts),
        "max_bits": math.log2(outcomes),
        "samples": float(samples),
    }
