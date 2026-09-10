"""
Continued fractions, and the Wiener attack on small RSA private exponents.

Owns the ``continued-fractions`` registry entry.

A continued fraction expands a rational (or real) as
``a0 + 1/(a1 + 1/(a2 + ...))``; its convergents are the best rational
approximations of each size. That approximation quality is a weapon: if an RSA
key uses a small private exponent ``d`` (someone optimising decryption speed),
then ``k/d`` appears among the convergents of ``e/n``, and Wiener's attack reads
``d`` straight off. The same expansion is behind the folklore claim that the
golden ratio is "the most irrational number" -- its convergents converge the
slowest -- which is a true statement about approximation, not a trading signal.

Public arithmetic; constant-time does not apply.

References: Wiener, *Cryptanalysis of short RSA secret exponents* (1990); HAC
sec. 8.2.2; Hardy & Wright on continued fractions.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

__all__ = [
    "continued_fraction",
    "convergents",
    "wiener_attack",
]


def continued_fraction(numerator: int, denominator: int) -> list[int]:
    """
    The continued-fraction expansion of ``numerator / denominator``.

    Returns the partial quotients ``[a0, a1, ...]``. This is the Euclidean
    algorithm's quotient sequence, so it terminates for any rational.
    ``denominator`` must be non-zero. Not constant time.
    """
    if denominator == 0:
        raise ValueError("denominator must be non-zero")
    quotients = []
    a, b = numerator, denominator
    while b:
        quotients.append(a // b)
        a, b = b, a - (a // b) * b
    return quotients


def convergents(quotients: list[int]) -> Iterator[tuple[int, int]]:
    """
    Yield the convergents ``(h_i, k_i)`` of a continued fraction in order.

    Uses the standard recurrence ``h_i = a_i h_{i-1} + h_{i-2}`` (and likewise
    for ``k``), which builds each best-approximation numerator/denominator from
    the two before it. Not constant time.
    """
    h_prev, h_prev2 = 1, 0
    k_prev, k_prev2 = 0, 1
    for a in quotients:
        h = a * h_prev + h_prev2
        k = a * k_prev + k_prev2
        yield h, k
        h_prev, h_prev2 = h, h_prev
        k_prev, k_prev2 = k, k_prev


def wiener_attack(e: int, n: int) -> int | None:
    """
    Recover the RSA private exponent ``d`` from ``(e, n)`` if it is small.

    Wiener's theorem: when ``d < n^(1/4) / 3`` the fraction ``k/d`` is a
    convergent of ``e/n``. This walks the convergents, and for each candidate
    ``k/d`` reconstructs ``phi = (e*d - 1) / k`` and checks it by solving the
    quadratic ``x^2 - (n - phi + 1) x + n = 0`` for integer roots ``p, q`` --
    the only test that confirms a guess rather than merely fitting the
    arithmetic. Returns ``d`` on success, ``None`` when no convergent yields a
    valid factorisation (the ordinary case for a properly-sized ``d``).

    A large ``d`` is not a failure of the attack; it is the attack correctly
    reporting the key is not vulnerable. Not constant time.
    """
    if e <= 0 or n <= 0:
        raise ValueError("e and n must be positive")
    for k, d in convergents(continued_fraction(e, n)):
        if k == 0 or (e * d - 1) % k != 0:
            continue
        phi = (e * d - 1) // k
        # p and q are the roots of x^2 - (n - phi + 1) x + n.
        s = n - phi + 1
        discriminant = s * s - 4 * n
        if discriminant < 0:
            continue
        root = math.isqrt(discriminant)
        if root * root != discriminant:
            continue
        # When the discriminant is a perfect square, (s - root)(s + root) = 4n
        # is even and s, root share a parity, so s + root is necessarily even --
        # there is no odd case to guard against here.
        p, q = (s + root) // 2, (s - root) // 2
        if p * q == n and p > 1 and q > 1:
            return d
    return None
