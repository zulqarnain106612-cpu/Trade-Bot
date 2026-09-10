"""
Baillie-PSW primality testing, and the Lucas sequences its second half needs.

Owns two registry entries:

* ``primality-testing-bpsw`` -- the gate every generated prime passes.
* ``lucas-sequences`` -- the Fibonacci-family recurrence underneath the Lucas
  half, which is the one place in this project where such an object does real
  cryptographic work rather than decorating a chart.

BPSW is a base-2 strong probable-prime test followed by a strong Lucas test
with Selfridge's Method A parameters. The two halves fail on disjoint-looking
sets of composites: no composite is known to pass both, none exists below
2**64, and the pair has stood since 1980. It is still, formally, a probable
prime test -- :func:`is_probable_prime` is named for what it proves, and
nothing here should be read as a proof of primality.

What makes it worth the name is that it is not probabilistic in the Miller-Rabin
sense: the parameters are chosen deterministically from ``n``, so the answer is
a function of ``n`` alone. There is no random base for an adversary to lose a
coin flip against, and no seed to reproduce when a result is disputed.

**Constant time: none of it.** Trial division exits early on a small factor,
the Lucas parameter search runs a data-dependent number of rounds, and CPython
integers branch on width. Nothing here may gate a secret. Every public function
repeats this.

References: Baillie & Wagstaff, *Lucas Pseudoprimes*, Math. Comp. 35 (1980);
Selfridge's Method A as described there; FIPS 186-5 App. B.3; HAC 4.2-4.4.
"""

from __future__ import annotations

import math

from .residues import jacobi_symbol

__all__ = [
    "SMALL_PRIMES",
    "is_probable_prime",
    "lucas_sequence",
    "selfridge_parameters",
    "strong_lucas_probable_prime",
    "strong_probable_prime",
]


# Every prime below 256. Trial division against these settles the great
# majority of composites for the cost of a few remainders, and -- more
# importantly -- guarantees the arguments handed to the two probable-prime
# tests below are odd and coprime to all of them, which their preconditions
# assume.
SMALL_PRIMES: tuple[int, ...] = (
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67,
    71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149,
    151, 157, 163, 167, 173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229,
    233, 239, 241, 251,
)


def _check_odd_modulus(n: int, what: str) -> None:
    """Shared argument validation. Not constant time."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"{what} requires an int, got {type(n).__name__}")
    if n < 3:
        raise ValueError(f"{what} requires n >= 3, got {n}")
    if n % 2 == 0:
        raise ValueError(f"{what} requires an odd n, got {n}")


def strong_probable_prime(n: int, base: int) -> bool:
    """
    Whether odd ``n >= 3`` is a strong probable prime to ``base``.

    Writes ``n - 1 = d * 2**s`` with ``d`` odd and checks that ``base**d`` is
    ``1``, or that some ``base**(d * 2**r)`` for ``r < s`` is ``-1``. A prime
    satisfies this for every base coprime to it; a composite satisfies it for
    at most a quarter of them.

    A base that is ``0`` or ``+-1`` modulo ``n`` is rejected rather than
    answered: those satisfy the congruence for every ``n``, so a ``True`` from
    them means nothing, and returning it would be a test that cannot fail.
    Not constant time.
    """
    _check_odd_modulus(n, "strong_probable_prime")
    base %= n
    if base in (0, 1, n - 1):
        raise ValueError(
            f"base {base} mod {n} is trivial; every n passes it, so the result "
            "would carry no information"
        )

    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1

    x = pow(base, d, n)
    if x in (1, n - 1):
        return True
    for _ in range(s - 1):
        x = (x * x) % n
        if x == n - 1:
            return True
    return False


def lucas_sequence(n: int, p: int, q: int, k: int) -> tuple[int, int, int]:
    """
    Return ``(U_k, V_k, q**k)`` modulo ``n`` for the Lucas sequences of
    ``(p, q)``.

    ``U`` and ``V`` satisfy ``x_(m+1) = p*x_m - q*x_(m-1)`` from
    ``(U_0, U_1) = (0, 1)`` and ``(V_0, V_1) = (2, p)``. With ``p, q = 1, -1``
    they are the Fibonacci and Lucas numbers, which is the family the registry
    entry names.

    Computed by binary laddering on the doubling identities rather than by
    iterating the recurrence, so the cost is logarithmic in ``k`` and the
    sequence index can be as large as ``n``. ``k`` must be non-negative;
    ``n`` odd and at least 3, since the halving step below needs 2 invertible
    modulo ``n``. Not constant time -- the ladder branches on the bits of ``k``.
    """
    _check_odd_modulus(n, "lucas_sequence")
    if k < 0:
        raise ValueError(f"lucas_sequence requires k >= 0, got {k}")

    u, v, qk = 0, 2, 1
    if k == 0:
        return u, v, qk

    # Halving is exact modulo an odd n: if x is odd, x + n is even and
    # congruent, so (x + n) // 2 is x * 2**-1 mod n without an inversion.
    def half(x: int) -> int:
        return (x + n) // 2 if x % 2 else x // 2

    p %= n
    q %= n
    for bit in bin(k)[2:]:
        # Doubling: U_2m = U_m V_m, V_2m = V_m**2 - 2 q**m.
        u, v, qk = (u * v) % n, (v * v - 2 * qk) % n, (qk * qk) % n
        if bit == "1":
            # Incrementing: U_(m+1) = (p U_m + V_m) / 2, V_(m+1) = (D U_m + p V_m) / 2
            # with D = p**2 - 4q, both halved by the exact rule above.
            u, v = half(p * u + v) % n, half((p * p - 4 * q) * u + p * v) % n
            qk = (qk * q) % n
    return u, v, qk


def selfridge_parameters(n: int) -> tuple[int, int, int]:
    """
    Selfridge's Method A parameters ``(d, p, q)`` for the Lucas test on ``n``.

    Scans ``d`` over ``5, -7, 9, -11, ...`` for the first with Jacobi symbol
    ``(d/n) == -1``, then takes ``p = 1`` and ``q = (1 - d) / 4``. The symbol
    being ``-1`` is what makes the test meaningful: it puts the Lucas sequence
    in the quadratic extension rather than back in the base field.

    A symbol of ``0`` for some ``|d| != n`` means ``d`` shares a factor with
    ``n``, which is a *proof* that ``n`` is composite -- strictly more than the
    Lucas test itself would establish. Rather than discard it, that ``d`` is
    returned with ``p = q = 0`` as a sentinel: "composite, and here is a number
    sharing a divisor with it". Callers must check for ``p == 0`` before using
    the parameters, and :func:`strong_lucas_probable_prime` does.

    ``n`` must be odd, at least 3, and not a perfect square -- the scan never
    terminates for a square, since every ``d`` is then a residue. Squares are
    screened by the caller (:func:`strong_lucas_probable_prime`) before this
    runs. Not constant time.
    """
    _check_odd_modulus(n, "selfridge_parameters")
    d = 5
    while True:
        symbol = jacobi_symbol(d, n)
        if symbol == -1:
            return d, 1, (1 - d) // 4
        if symbol == 0 and abs(d) != n:
            return d, 0, 0
        if abs(d) > 1 << 20:  # pragma: no cover - only a perfect square gets here
            raise ValueError(
                f"no Lucas parameter found for {n}; it is a perfect square and "
                "must be screened before calling this"
            )
        d = -d + (2 if d < 0 else -2)


def _is_perfect_square(n: int) -> bool:
    """Exact integer square test, via ``math.isqrt``. Not constant time."""
    if n < 0:
        return False
    root = math.isqrt(n)
    return root * root == n


def strong_lucas_probable_prime(n: int) -> bool:
    """
    Whether odd ``n >= 3`` is a strong Lucas probable prime, Method A.

    Rejects perfect squares outright: the parameter scan cannot terminate for
    one, because every ``d`` is a quadratic residue modulo a square. This is
    not an optimisation, it is what keeps :func:`selfridge_parameters` from
    looping forever.

    With ``n + 1 = d * 2**s`` and ``d`` odd, ``n`` passes when ``U_d == 0`` or
    some ``V_(d * 2**r)`` for ``r < s`` is ``0`` modulo ``n``. Not constant time.
    """
    _check_odd_modulus(n, "strong_lucas_probable_prime")
    if _is_perfect_square(n):
        return False

    _disc, p, q = selfridge_parameters(n)
    if p == 0:  # a divisor of n fell out of the parameter scan
        return False

    k = n + 1
    s = 0
    while k % 2 == 0:
        k //= 2
        s += 1

    u, v, qk = lucas_sequence(n, p, q, k)
    if u == 0 or v == 0:
        return True
    for _ in range(s - 1):
        v = (v * v - 2 * qk) % n
        if v == 0:
            return True
        qk = (qk * qk) % n
    return False


def is_probable_prime(n: int) -> bool:
    """
    Baillie-PSW: whether ``n`` is prime, with no known counterexample.

    Exact for every ``n`` this function can be handed below 2**64, where the
    combination has been verified exhaustively. Above that it remains a
    probable-prime test with no known composite passing it -- which is what
    every serious library ships, and why the name says "probable".

    Order of work, cheapest first: values below 2, the small primes by table,
    trial division by them, then the base-2 strong test, then the strong Lucas
    test. Not constant time -- an early exit on a small factor is visible in
    the timing, so never call this on a secret.
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"is_probable_prime requires an int, got {type(n).__name__}")
    if n < 2:
        return False
    for small in SMALL_PRIMES:
        if n == small:
            return True
        if n % small == 0:
            return False
    if n < SMALL_PRIMES[-1] ** 2:
        # Trial division by every prime below 256 already settles anything
        # under 251**2 -- a composite there must have a factor below its root.
        return True
    return strong_probable_prime(n, 2) and strong_lucas_probable_prime(n)
