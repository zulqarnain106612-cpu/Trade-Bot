"""
Integer factorisation: the problem RSA's security rests on.

Owns the ``prime-factorisation-rsa`` registry entry.

RSA is secure exactly as long as recovering ``p`` and ``q`` from ``n = p*q`` is
hard, and this module is the defender's copy of the attack -- run it on your own
modulus to confirm it does not fall to the cheap methods. It combines the three
that catch a badly generated key:

* **trial division** by small primes, which finds a factor a weak key-gen left
  in;
* **Fermat's method**, which factors in one step when ``p`` and ``q`` are close
  -- the classic failure of sampling both primes from a narrow range;
* **Pollard's rho**, a general-purpose method whose cost is ``O(n^(1/4))``, fine
  for a smallish or structured modulus and hopeless against a proper 2048-bit
  one, which is the point.

It is emphatically **not** the number field sieve; a real 2048-bit RSA modulus
does not factor here, and :func:`factorize` says so by giving up rather than
hanging. Primality of a found factor is decided by the registry's own
:func:`~src.mathcore.numbertheory.primality.is_probable_prime`, not a private
copy.

Public-data arithmetic; constant-time does not apply.

References: HAC ch. 3; Pollard, *A Monte Carlo method for factorization* (1975);
Fermat's method as in any number-theory text.
"""

from __future__ import annotations

import math

from .primality import SMALL_PRIMES, is_probable_prime

__all__ = [
    "factorize",
    "fermat_factor",
    "pollard_rho",
    "trial_division",
]


def trial_division(n: int, bound: int | None = None) -> list[int]:
    """
    Factor ``n`` by dividing out primes up to ``bound`` (default: 256).

    Returns the small prime factors found, with multiplicity, and leaves the
    unfactored cofactor to the caller via :func:`factorize`. This alone settles
    a key whose generation left a small factor in. Not constant time.
    """
    if n < 1:
        raise ValueError(f"n must be positive, got {n}")
    factors: list[int] = []
    remaining = n
    primes = SMALL_PRIMES if bound is None else [p for p in SMALL_PRIMES if p <= bound]
    for p in primes:
        while remaining % p == 0:
            factors.append(p)
            remaining //= p
    return factors


def fermat_factor(n: int, max_steps: int = 1 << 20) -> tuple[int, int] | None:
    """
    Factor odd ``n`` via Fermat's method, or ``None`` within ``max_steps``.

    Writes ``n = a^2 - b^2 = (a-b)(a+b)`` by walking ``a`` up from
    ``ceil(sqrt(n))`` until ``a^2 - n`` is a perfect square. When ``p`` and ``q``
    are close this terminates almost immediately -- which is exactly why
    sampling both RSA primes from a narrow window is a break. Far-apart factors
    make it slow, so the step budget bounds it rather than letting it run to
    ``n/2``. Even ``n`` and perfect squares are handled before the loop.
    Not constant time.
    """
    if n <= 0 or n % 2 == 0:
        raise ValueError(f"fermat_factor requires an odd positive n, got {n}")
    a = math.isqrt(n)
    if a * a == n:
        return a, a
    a += 1
    for _ in range(max_steps):
        b2 = a * a - n
        b = math.isqrt(b2)
        if b * b == b2:
            return a - b, a + b
        a += 1
    return None


def pollard_rho(n: int, *, seed: int = 2, max_iterations: int = 1 << 20) -> int | None:
    """
    Find a non-trivial factor of composite ``n`` by Pollard's rho, or ``None``.

    Floyd cycle detection on ``x -> x^2 + 1 (mod n)``; the GCD of the tortoise-
    hare gap with ``n`` yields a factor with cost about ``n^(1/4)``. Returns
    ``None`` if this ``seed``'s sequence closes its cycle without a factor, or
    if ``max_iterations`` is reached first -- the caller (:func:`factorize`)
    then retries with another seed or gives up. The iteration cap is essential:
    on a modulus with no factor below ``n^(1/4)`` the cycle length is
    astronomical, and without the cap this would run effectively forever rather
    than admit defeat. Even ``n`` short-circuits to 2. Not constant time.
    """
    if n < 2:
        raise ValueError(f"pollard_rho requires n >= 2, got {n}")
    if n % 2 == 0:
        return 2

    def f(x: int) -> int:
        return (x * x + 1) % n

    tortoise = hare = seed
    for _ in range(max_iterations):
        tortoise = f(tortoise)
        hare = f(f(hare))
        if tortoise == hare:
            return None  # cycle closed without a factor; caller reseeds
        divisor = math.gcd(abs(tortoise - hare), n)
        if divisor != 1:
            return divisor if divisor != n else None
    return None  # budget exhausted: no factor within reach


def factorize(
    n: int, *, fermat_steps: int = 1 << 16, pollard_iterations: int = 1 << 20
) -> list[int]:
    """
    Fully factor ``n`` into primes, or raise if it resists the cheap methods.

    Order: trial division, then Fermat (cheap when the factors are close), then
    Pollard's rho with reseeding, recursing on composite cofactors and testing
    primality with :func:`is_probable_prime`. Returns the prime factors sorted,
    with multiplicity, so their product is ``n``.

    Raises :class:`ValueError` when a cofactor survives all of this -- a real
    RSA modulus will -- rather than hanging or returning a partial answer
    presented as complete. That refusal is the honest boundary: this module
    factors weak or small moduli and openly cannot factor strong ones.
    Not constant time.
    """
    if n < 1:
        raise ValueError(f"n must be positive, got {n}")
    if n == 1:
        return []

    factors = trial_division(n)
    remaining = n
    for p in factors:
        remaining //= p

    if remaining == 1:
        return sorted(factors)

    factors.extend(_factor_cofactor(remaining, fermat_steps, pollard_iterations))
    return sorted(factors)


def _factor_cofactor(n: int, fermat_steps: int, pollard_iterations: int) -> list[int]:
    """
    Recursively factor a cofactor already stripped of small primes.

    Only ever called with ``n > 1`` -- :func:`factorize` returns early for 1 and
    the recursion splits into factors both above 1 -- so there is no ``n == 1``
    guard; adding one would be an unreachable branch.
    """
    if is_probable_prime(n):
        return [n]

    # Try Fermat first: one step when the factors are close.
    fermat = fermat_factor(n, fermat_steps) if n % 2 else None
    if fermat is not None and 1 < fermat[0] < n:
        left, right = fermat
        return _factor_cofactor(left, fermat_steps, pollard_iterations) + _factor_cofactor(
            right, fermat_steps, pollard_iterations
        )

    # Then Pollard's rho, reseeding on failure.
    for seed in range(2, 12):
        divisor = pollard_rho(n, seed=seed, max_iterations=pollard_iterations)
        if divisor is not None and 1 < divisor < n:
            return _factor_cofactor(divisor, fermat_steps, pollard_iterations) + _factor_cofactor(
                n // divisor, fermat_steps, pollard_iterations
            )

    raise ValueError(
        f"could not factor the cofactor {n} with trial division, Fermat, or "
        "Pollard's rho; it has no small or close factors, which is the case a "
        "properly generated RSA modulus is built to be. This module is not the "
        "number field sieve and does not claim to factor strong moduli."
    )
