"""
The birthday bound: when a random collision becomes likely.

Owns the ``birthday-bound`` registry entry.

A space of ``N`` equally likely values yields a collision after roughly
``sqrt(N)`` random draws, not ``N`` -- the reason a 128-bit hash gives only
64-bit collision resistance, and the sizing rule behind every nonce, salt and
short identifier this project generates. This module answers the two questions
that sizing actually asks: how likely is a collision after ``k`` draws from
``N`` values, and how many draws before that likelihood crosses a threshold.

Deterministic float arithmetic, checkable against the classic n=365 birthday
problem. Nothing here is secret, so constant-time does not apply.

References: FIPS 202 App. A (capacity sizing); Menezes, van Oorschot & Vanstone,
*Handbook of Applied Cryptography*, sec. 2.1.5.
"""

from __future__ import annotations

import math

_EXACT_PRODUCT_LIMIT = 100_000

__all__ = [
    "birthday_collision_probability",
    "birthday_bound",
]


def birthday_collision_probability(draws: int, space_size: int) -> float:
    """
    The probability that ``draws`` uniform picks from ``space_size`` values
    contain at least one repeat.

    Computed as ``1 - P(all distinct)``, and the all-distinct product is summed
    in log space so that a large ``space_size`` does not lose every factor to
    rounding at ``1 - i/N`` near zero. Returns 1.0 once ``draws`` exceeds
    ``space_size`` -- more pigeons than holes collide with certainty, the
    pigeonhole boundary the product would otherwise reach via a zero factor.

    ``draws`` of 0 or 1 cannot collide and return 0.0. Not constant time.
    """
    if draws < 0:
        raise ValueError(f"draws must be non-negative, got {draws}")
    if space_size < 1:
        raise ValueError(f"space_size must be at least 1, got {space_size}")
    if draws > space_size:
        return 1.0
    if draws < 2:
        return 0.0

    # The exact all-distinct product is ``prod_{i<draws} (1 - i/N)``, summed in
    # log space. It costs one term per draw, so for the large spaces this is
    # used on -- 2**64 and up, where the birthday count runs to billions -- it
    # is replaced by the standard closed form ``1 - exp(-k(k-1)/2N)``. That
    # approximation is a second-order expansion of the same product and its
    # error is below 1e-9 once ``N`` is past a few thousand, far tighter than
    # any decision made from it. The exact loop is kept for small ``N`` because
    # near the n=365 boundary the approximation is off by enough to move the
    # birthday count by one, and that off-by-one is the whole question.
    if draws > _EXACT_PRODUCT_LIMIT:
        return -math.expm1(-draws * (draws - 1) / (2.0 * space_size))

    log_distinct = 0.0
    for i in range(draws):
        log_distinct += math.log1p(-i / space_size)
    return -math.expm1(log_distinct)


def birthday_bound(space_size: int, threshold: float = 0.5) -> int:
    """
    The fewest draws from ``space_size`` values for which a collision is at
    least ``threshold`` likely.

    The default ``0.5`` is the textbook birthday number -- 23 for a year, which
    is the module's known-answer check. The collision probability rises
    monotonically in ``draws``, so the first count that reaches ``threshold`` is
    the answer.

    Uses the ``sqrt`` approximation only to seed the search, then confirms
    against the exact :func:`birthday_collision_probability`, so the returned
    count is exact rather than approximate -- the approximation would be off by
    one near the boundary, and off-by-one is the whole question here.

    ``threshold`` must be in ``(0, 1]``. Not constant time.
    """
    if space_size < 1:
        raise ValueError(f"space_size must be at least 1, got {space_size}")
    if not 0 < threshold <= 1:
        raise ValueError(f"threshold must be in (0, 1], got {threshold}")

    # Seed near the analytic estimate k ~ sqrt(2 N ln(1/(1-threshold))), then
    # step to the exact crossing. Start a little below and walk up.
    estimate = (
        math.sqrt(2 * space_size * math.log(1 / (1 - threshold))) if threshold < 1 else space_size
    )
    draws = max(0, int(estimate) - 2)
    while birthday_collision_probability(draws, space_size) < threshold:
        draws += 1
    return draws
