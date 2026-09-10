"""
Settlement risk: how many confirmations a payment of a given value needs.

Owns two registry entries:

* ``poisson-block-arrival`` -- blocks arrive as a Poisson process, so an
  attacker's private-chain progress while the network builds ``z`` blocks is
  Poisson-distributed.
* ``gamblers-ruin-doublespend`` -- given that head start, whether the attacker
  ever catches up is the classic gambler's-ruin problem, and Nakamoto's
  section 11 combines the two into a double-spend success probability.

The point of the module is :func:`required_depth`: it turns the flat
"six confirmations" convention into a function of what is actually at stake, so
a dust payment clears fast and a large one waits exactly as long as its value
warrants and no longer.

Everything here is deterministic float arithmetic -- probabilities, not
simulations -- so the results are reproducible and checkable against Nakamoto's
published table. Constant-time is not a concern: nothing here touches a secret.

References: Nakamoto, *Bitcoin: A Peer-to-Peer Electronic Cash System*, sec. 11;
Rosenfeld, *Analysis of hashrate-based double-spending* (2014); Grunspan &
Perez-Marco, *Double spend races* (2018).
"""

from __future__ import annotations

import math

__all__ = [
    "double_spend_probability",
    "poisson_pmf",
    "required_depth",
]


def poisson_pmf(k: int, mean: float) -> float:
    """
    The Poisson probability of exactly ``k`` events given expected ``mean``.

    ``exp(-mean) * mean**k / k!``, computed in log space so a large ``mean``
    does not overflow ``mean**k`` before the ``exp(-mean)`` damps it. ``mean``
    may be zero -- then all mass is at ``k == 0`` -- which is the ``z == 0`` and
    ``q == 0`` boundary the double-spend formula leans on.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if mean < 0:
        raise ValueError(f"mean must be non-negative, got {mean}")
    if mean == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mean + k * math.log(mean) - math.lgamma(k + 1))


def double_spend_probability(attacker_share: float, depth: int) -> float:
    """
    The probability an attacker with hashpower fraction ``attacker_share`` ever
    overtakes a transaction buried ``depth`` blocks deep.

    This is Nakamoto's section-11 calculation exactly: the attacker's progress
    while the honest chain adds ``depth`` blocks is Poisson with mean
    ``depth * q / p`` (``q = attacker_share``, ``p = 1 - q``), and for each
    possible head start the catch-up probability is the gambler's-ruin term
    ``(q/p)**(depth - k)``, capped at 1.

    Returns 1.0 for ``attacker_share >= 0.5`` -- a majority attacker overtakes
    any depth with certainty, which is the honest boundary rather than a number
    the Poisson sum would misreport -- and 1.0 for ``depth == 0``, since an
    unconfirmed transaction is not yet defended at all.

    ``attacker_share`` must be in ``[0, 1)``... and exactly ``0.5`` or above is
    treated as the certain-success majority case. Not constant time.
    """
    if not 0 <= attacker_share < 1:
        raise ValueError(f"attacker_share must be in [0, 1), got {attacker_share}")
    if depth < 0:
        raise ValueError(f"depth must be non-negative, got {depth}")
    if attacker_share >= 0.5:
        return 1.0
    if depth == 0:
        return 1.0

    q = attacker_share
    p = 1.0 - q
    ratio = q / p
    lam = depth * ratio

    total = 1.0
    for k in range(depth + 1):
        total -= poisson_pmf(k, lam) * (1.0 - ratio ** (depth - k))
    # Clamp: floating-point cancellation in the sum can nudge the result a
    # hair outside [0, 1], and a probability that reads as 1.0000000002 is a
    # bug waiting to trip a caller's own bounds check.
    return min(1.0, max(0.0, total))


def required_depth(
    attacker_share: float,
    value_at_risk: float,
    acceptable_loss: float,
    *,
    max_depth: int = 1000,
) -> int:
    """
    The fewest confirmations for which the *expected* loss to a double spend is
    at or below ``acceptable_loss``.

    Expected loss is ``double_spend_probability(attacker_share, depth) *
    value_at_risk``. Because the probability falls monotonically in ``depth``,
    the first depth that clears the bound is the answer, and this returns it.

    Monotone by construction in both arguments the exit gate names: a larger
    ``value_at_risk`` needs the probability driven lower, hence more depth; a
    larger ``attacker_share`` raises the probability at every depth, hence more
    depth. Those are asserted as properties in the tests, not sampled.

    ``max_depth`` bounds the search. Reaching it means the bound cannot be met
    for this attacker share -- for a near-50% attacker the probability decays
    so slowly that no practical depth suffices -- and that is raised, loudly,
    rather than returning a depth that does not actually clear the bound. A
    silent cap would tell a caller a large payment was safe at a depth where it
    is not.

    ``acceptable_loss`` of zero is only reachable in the limit and so is
    refused: it would always hit ``max_depth``. Not constant time.
    """
    if value_at_risk < 0:
        raise ValueError(f"value_at_risk must be non-negative, got {value_at_risk}")
    if acceptable_loss <= 0:
        raise ValueError(
            f"acceptable_loss must be positive, got {acceptable_loss}; a zero "
            "tolerance is only met in the limit and has no finite depth"
        )
    if max_depth < 0:
        raise ValueError(f"max_depth must be non-negative, got {max_depth}")

    # A payment worth no more than the tolerance is safe with zero confirmations.
    if value_at_risk <= acceptable_loss:
        return 0

    for depth in range(max_depth + 1):
        if double_spend_probability(attacker_share, depth) * value_at_risk <= acceptable_loss:
            return depth
    raise ValueError(
        f"no depth up to {max_depth} brings the expected loss of {value_at_risk} "
        f"to {acceptable_loss} against a {attacker_share:.0%} attacker; the share "
        "is too close to half for any practical confirmation count to settle"
    )
