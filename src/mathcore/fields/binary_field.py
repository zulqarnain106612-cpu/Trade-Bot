"""
Arithmetic in GF(2^n), the binary half of the ``finite-fields`` registry entry.

Elements are integers used as bit vectors: bit ``i`` is the coefficient of
``x**i``. Addition is XOR, which is why symmetric primitives like it -- there
are no carries to propagate and no timing variation from them.

The field is defined by an irreducible polynomial of degree ``n``, given the
same way: :data:`AES_POLY` is ``0x11B``, meaning ``x**8 + x**4 + x**3 + x + 1``,
the polynomial FIPS 197 specifies for the AES S-box.

**Irreducibility is verified, not trusted.** A reducible modulus gives a ring
with zero divisors rather than a field: multiplication still works, ``inv``
still returns something for most inputs, and the failure surfaces as an
occasional wrong answer far from here. Rabin's test runs once per
:class:`BinaryField` construction, which is cheap and settles it.

**Constant time: none of it.** The inversion is an extended Euclidean loop
whose trip count depends on its input, and CPython integers branch on width.
Nothing here may gate a secret -- which matters more in this field than in
GF(p), because GF(2^8) inversion *is* the AES S-box and a timing-variable
S-box is a textbook cache-timing key recovery. A production AES uses a table
or bitslicing, not this. This module is for known-answer checking and for the
algebra that other registry entries build on.

References: FIPS 197 sec. 4.2 and app. A; Rabin, *Probabilistic Algorithms in
Finite Fields*, SIAM J. Comput. 9 (1980); HAC 2.6 and 14.7.
"""

from __future__ import annotations

__all__ = [
    "AES_POLY",
    "GCM_POLY",
    "BinaryField",
]


# x**8 + x**4 + x**3 + x + 1 -- FIPS 197, the AES S-box field.
AES_POLY = 0x11B

# x**128 + x**7 + x**2 + x + 1 -- the polynomial behind AES-GCM's GHASH. Note
# that GHASH itself uses a reversed bit order on top of this field; matching
# GCM byte-for-byte needs that convention too, which this module does not
# impose. What is offered here is the field, not the GHASH encoding.
GCM_POLY = (1 << 128) | 0x87


def _poly_mul(a: int, b: int) -> int:
    """Carry-less product of two GF(2) polynomials. Not constant time."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        b >>= 1
    return result


def _poly_divmod(a: int, b: int) -> tuple[int, int]:
    """
    Quotient and remainder of ``a`` by non-zero ``b`` over GF(2).

    Long division on bit vectors: repeatedly cancel the leading term.
    Not constant time.
    """
    if b == 0:
        raise ZeroDivisionError("polynomial division by the zero polynomial")
    quotient = 0
    degree_b = b.bit_length() - 1
    while a.bit_length() - 1 >= degree_b and a:
        shift = (a.bit_length() - 1) - degree_b
        quotient ^= 1 << shift
        a ^= b << shift
    return quotient, a


def _poly_mod(a: int, modulus: int) -> int:
    """Remainder of ``a`` modulo ``modulus`` over GF(2). Not constant time."""
    return _poly_divmod(a, modulus)[1]


def _poly_gcd(a: int, b: int) -> int:
    """Monic GCD of two GF(2) polynomials -- monic is automatic here, as the
    only non-zero coefficient value is 1. Not constant time."""
    while b:
        a, b = b, _poly_mod(a, b)
    return a


def _poly_powmod(base: int, exponent: int, modulus: int) -> int:
    """``base**exponent mod modulus`` over GF(2). Not constant time."""
    result = 1
    base = _poly_mod(base, modulus)
    while exponent:
        if exponent & 1:
            result = _poly_mod(_poly_mul(result, base), modulus)
        base = _poly_mod(_poly_mul(base, base), modulus)
        exponent >>= 1
    return result


def _prime_factors(n: int) -> set[int]:
    """The distinct prime factors of a small ``n >= 1``. Not constant time."""
    factors = set()
    d = 2
    while d * d <= n:
        while n % d == 0:
            factors.add(d)
            n //= d
        d += 1
    if n > 1:
        factors.add(n)
    return factors


def _is_irreducible(poly: int, degree: int) -> bool:
    """
    Rabin's irreducibility test for a degree-``degree`` polynomial over GF(2).

    ``f`` of degree ``n`` is irreducible exactly when ``x**(2**n) == x mod f``
    and ``gcd(x**(2**(n/q)) - x, f) == 1`` for every prime ``q`` dividing
    ``n``. The first condition says every root lies in GF(2^n); the second says
    none lies in a proper subfield, which is what would make ``f`` factor.

    Deterministic despite the paper's title -- the probabilistic part of that
    work is elsewhere. Not constant time.
    """
    for q in _prime_factors(degree):
        h = _poly_powmod(2, 1 << (degree // q), poly) ^ 2  # x**(2**(n/q)) - x
        if _poly_gcd(h, poly) != 1:
            return False
    return _poly_powmod(2, 1 << degree, poly) == 2


class BinaryField:
    """
    GF(2^n) defined by an irreducible polynomial over GF(2).

    Elements are ``int`` bit vectors below ``2**n``; every method reduces its
    arguments first, so an unreduced input is accepted rather than silently
    misinterpreted.

    No method is constant time; see the module docstring.
    """

    __slots__ = ("degree", "modulus")

    def __init__(self, modulus: int, degree: int | None = None) -> None:
        if not isinstance(modulus, int) or isinstance(modulus, bool):
            raise TypeError(f"modulus must be an int, got {type(modulus).__name__}")
        if modulus < 2:
            raise ValueError(f"modulus must be a polynomial of degree >= 1, got {modulus}")

        implied = modulus.bit_length() - 1
        if degree is None:
            degree = implied
        elif degree != implied:
            raise ValueError(
                f"modulus 0x{modulus:X} has degree {implied}, not the declared {degree}"
            )
        if not _is_irreducible(modulus, degree):
            raise ValueError(
                f"0x{modulus:X} is reducible over GF(2); it defines a ring with zero "
                "divisors, not a field, and inversion in it is not well defined"
            )

        self.modulus = modulus
        self.degree = degree

    @property
    def order(self) -> int:
        """The number of elements, ``2**degree``. Not constant time."""
        return 1 << self.degree

    def __repr__(self) -> str:
        return f"BinaryField(modulus=0x{self.modulus:X}, degree={self.degree})"

    def reduce(self, a: int) -> int:
        """Reduce a bit vector into the field. Not constant time."""
        if a < 0:
            raise ValueError(f"GF(2^n) elements are non-negative bit vectors, got {a}")
        return _poly_mod(a, self.modulus)

    def add(self, a: int, b: int) -> int:
        """
        Sum in GF(2^n), which is XOR.

        Subtraction is the same operation -- every element is its own additive
        inverse -- so this module offers no ``sub`` or ``neg``. Adding them as
        aliases would suggest a distinction the field does not have.
        Not constant time.
        """
        return self.reduce(a) ^ self.reduce(b)

    def mul(self, a: int, b: int) -> int:
        """Product in GF(2^n): carry-less multiply, then reduce. Not constant time."""
        return _poly_mod(_poly_mul(self.reduce(a), self.reduce(b)), self.modulus)

    def square(self, a: int) -> int:
        """``a**2`` in GF(2^n). Not constant time."""
        return self.mul(a, a)

    def pow(self, a: int, e: int) -> int:
        """
        ``a**e`` in GF(2^n), with negative ``e`` inverting first.

        Not constant time, and it leaks the exponent through its branch
        pattern.
        """
        if e < 0:
            return _poly_powmod(self.inv(a), -e, self.modulus)
        return _poly_powmod(self.reduce(a), e, self.modulus)

    def inv(self, a: int) -> int:
        """
        Multiplicative inverse, by the extended Euclidean algorithm over GF(2).

        Raises :class:`ZeroDivisionError` for zero. In GF(2^8) this function is
        the algebraic core of the AES S-box, which is why the module docstring
        insists it must never be used on real key material: its running time
        depends on the value being inverted.

        Not constant time.
        """
        a = self.reduce(a)
        if a == 0:
            raise ZeroDivisionError("0 has no multiplicative inverse in GF(2^n)")

        # Track only the cofactor of a; the cofactor of the modulus is unused.
        old_r, r = a, self.modulus
        old_s, s = 1, 0
        while r:
            quotient, remainder = _poly_divmod(old_r, r)
            old_r, r = r, remainder
            old_s, s = s, old_s ^ _poly_mul(quotient, s)
        return _poly_mod(old_s, self.modulus)

    def div(self, a: int, b: int) -> int:
        """``a * b**-1`` in GF(2^n); raises if ``b`` is zero. Not constant time."""
        return self.mul(a, self.inv(b))
