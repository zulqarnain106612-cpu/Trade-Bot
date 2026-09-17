"""
Quadratic residues and square roots modulo a prime.

Owns two registry entries:

* ``quadratic-residues`` -- the Legendre and Jacobi symbols, which decide
  whether a square root exists before anyone tries to compute one.
* ``tonelli-shanks`` -- the square root itself, run on every compressed public
  key this project parses.

The two are deliberately in one module because they are one decision procedure
split in half: the symbol says whether the root exists, the algorithm produces
it, and a caller that skips the first half gets a plausible-looking value that
is not a root. :func:`sqrt_mod_prime` therefore never returns a wrong answer
for a non-residue -- it returns ``None``.

**Constant time: none of it.** The Jacobi symbol's reduction loop, the
non-residue search in Tonelli-Shanks, and CPython's variable-width integers all
branch on their inputs. Nothing here may decide anything about a secret. Each
public function repeats this, because the roadmap requires the property to be
stated rather than inferred.

References: HAC 2.4 and 3.5; Cohen, *A Course in Computational Algebraic Number
Theory*, alg. 1.4.10; Tonelli (1891); Shanks (1973).
"""

from __future__ import annotations

__all__ = [
    "is_quadratic_residue",
    "jacobi_symbol",
    "legendre_symbol",
    "sqrt_mod_prime",
]


def jacobi_symbol(a: int, n: int) -> int:
    """
    The Jacobi symbol ``(a/n)`` for odd ``n >= 1``.

    Defined for any odd positive ``n``, prime or not -- that generality is the
    point, since the Lucas half of a BPSW primality test needs the symbol for a
    number whose primality is exactly what is in question.

    Returns ``0`` when ``gcd(a, n) > 1``, else ``1`` or ``-1``. Note that
    ``1`` does **not** imply ``a`` is a square modulo a composite ``n``; only
    for prime ``n`` does the symbol decide residuosity, which is why
    :func:`is_quadratic_residue` takes a prime and this function does not
    pretend to answer that question.

    Even or non-positive ``n`` is rejected rather than coerced: the symbol is
    undefined there, and returning any int would be a fabricated answer.
    Not constant time -- the loop count depends on both arguments.
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"n must be an int, got {type(n).__name__}")
    if not isinstance(a, int) or isinstance(a, bool):
        raise TypeError(f"a must be an int, got {type(a).__name__}")
    if n <= 0 or n % 2 == 0:
        raise ValueError(f"the Jacobi symbol requires an odd n >= 1, got {n}")

    a %= n
    result = 1
    while a != 0:
        while a % 2 == 0:
            a //= 2
            # (2/n) is -1 exactly when n is 3 or 5 mod 8.
            if n % 8 in (3, 5):
                result = -result
        a, n = n, a
        # Quadratic reciprocity: the flip costs a sign when both are 3 mod 4.
        if a % 4 == 3 and n % 4 == 3:
            result = -result
        a %= n
    return result if n == 1 else 0


def legendre_symbol(a: int, p: int) -> int:
    """
    The Legendre symbol ``(a/p)`` for an odd prime ``p``.

    Primality of ``p`` is a precondition, not a check -- the test is a separate
    registry entry (``primality-testing-bpsw``) with its own owning module, and
    a private copy here would be the unregistered implementation the registry
    exists to prevent. What is checked is oddness and ``p >= 3``, which is what
    the computation itself requires.

    Computed as the Jacobi symbol, which coincides with the Legendre symbol
    whenever the modulus is prime. Not constant time.
    """
    if p < 3:
        raise ValueError(f"the Legendre symbol requires an odd prime p >= 3, got {p}")
    return jacobi_symbol(a, p)


def is_quadratic_residue(a: int, p: int) -> bool:
    """
    Whether ``a`` is a non-zero square modulo the odd prime ``p``.

    ``a == 0 (mod p)`` is **not** a quadratic residue by this definition: zero
    is a square, but it is excluded from the residue classes because callers
    use this to decide whether a square root exists in the multiplicative
    group. :func:`sqrt_mod_prime` handles zero separately and correctly.
    Not constant time.
    """
    return legendre_symbol(a, p) == 1


def sqrt_mod_prime(a: int, p: int) -> int | None:
    """
    A square root of ``a`` modulo the odd prime ``p``, or ``None`` if none exists.

    Returns the root ``r`` with ``r*r % p == a % p``; the other root is
    ``p - r``. Which of the two is returned is not specified beyond being
    deterministic for a given ``(a, p)``, so a caller needing a canonical
    choice (an even y for a compressed key, say) must pick between ``r`` and
    ``p - r`` itself rather than assume this returns the smaller.

    ``None`` for a non-residue is the whole safety property of this function.
    Tonelli-Shanks run on a non-residue does not fail loudly -- it terminates
    with a value that squares to something else -- so the Legendre symbol is
    checked first, always, and cannot be skipped by a caller.

    Three cases:

    * ``a == 0``: the root is ``0``, and the general algorithm does not cover
      it because zero is outside the multiplicative group.
    * ``p == 3 (mod 4)``: ``a**((p+1)/4)`` is the root directly. This covers
      secp256k1 and every other curve prime this project parses compressed
      keys for, so it is the path that actually runs in production.
    * otherwise: full Tonelli-Shanks, needed for ``p == 1 (mod 4)``.

    Primality of ``p`` is a precondition, as for :func:`legendre_symbol`.
    Not constant time: the non-residue search and the loop trip count both
    depend on the input.
    """
    if p < 3:
        raise ValueError(f"sqrt_mod_prime requires an odd prime p >= 3, got {p}")
    if p % 2 == 0:
        raise ValueError(f"sqrt_mod_prime requires an odd prime, got {p}")

    a %= p
    if a == 0:
        return 0
    if legendre_symbol(a, p) != 1:
        return None
    if p % 4 == 3:
        return pow(a, (p + 1) // 4, p)

    # Write p - 1 = q * 2**s with q odd.
    q = p - 1
    s = 0
    while q % 2 == 0:
        q //= 2
        s += 1

    # Any quadratic non-residue works as the generator of the 2-Sylow part.
    # Trial from 2 upward terminates fast because half of all residues qualify;
    # the bound is a guard against a non-prime p slipping past the precondition
    # and turning the search into an infinite loop.
    z = 2
    while z < p and legendre_symbol(z, p) != -1:
        z += 1
    if z == p:  # pragma: no cover - unreachable for a prime p
        raise ValueError(f"{p} has no quadratic non-residue; it is not prime")

    m = s
    c = pow(z, q, p)
    t = pow(a, q, p)
    r = pow(a, (q + 1) // 2, p)

    while t != 1:
        # Find the least i < m with t**(2**i) == 1.
        i = 0
        t2 = t
        while t2 != 1:
            t2 = (t2 * t2) % p
            i += 1
        b = pow(c, 1 << (m - i - 1), p)
        m = i
        c = (b * b) % p
        t = (t * c) % p
        r = (r * b) % p

    return r
