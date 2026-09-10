"""
Arithmetic in GF(p) for an odd prime modulus.

Owns two registry entries:

* ``finite-fields`` (jointly with ``binary_field``) -- the prime half.
* ``pseudo-mersenne-primes`` -- reduction for moduli of the shape ``2^k - c``,
  which is what makes secp256k1 and Curve25519 fast enough to verify per
  transaction.

Three reduction strategies live here, and they exist for different reasons:

``Reduction.PYTHON``
    ``x % p``. CPython's big-integer remainder, which is the reference every
    other strategy is differentially tested against.
``Reduction.BARRETT``
    HAC 14.42 with base 2. Replaces division by two multiplications and a
    shift once ``mu`` is precomputed. Correct for any ``0 <= x < 2^(2k)``.
``Reduction.MONTGOMERY``
    Operands are held in Montgomery form ``a*R mod p``. Products cost one
    multiplication plus one reduction with no division at all, which is the
    representation a scalar multiplication ladder wants.
``Reduction.PSEUDO_MERSENNE``
    Shift-and-add folding for a modulus of the shape ``2^k - c``. Selecting it
    for a modulus without that shape is rejected at construction rather than
    silently downgraded, so the cost of a field is what its declaration says.

**Constant time: none of it.** CPython's integers are variable-width and its
multiplication, remainder and comparison all branch on operand size, so no
function in this module is constant time and none may guard a secret against
a timing adversary. Each public function repeats this in its own docstring
because the roadmap requires the property to be stated, not assumed. When a
constant-time path is needed it will be a separate, explicitly-typed backend;
until it exists, treat every value passed through here as public.

References: HAC ch. 14; Montgomery, *Modular Multiplication Without Trial
Division*, Math. Comp. 44 (1985); SEC 2 v2; RFC 7748.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = [
    "CURVE25519_P",
    "NIST_P256_P",
    "SECP256K1_P",
    "PrimeField",
    "PseudoMersenne",
    "Reduction",
    "as_pseudo_mersenne",
]


SECP256K1_P = (1 << 256) - (1 << 32) - 977
CURVE25519_P = (1 << 255) - 19
NIST_P256_P = (1 << 256) - (1 << 224) + (1 << 192) + (1 << 96) - 1


class Reduction(enum.Enum):
    """Which modular reduction a :class:`PrimeField` uses for ``mul``."""

    PYTHON = "python"
    BARRETT = "barrett"
    MONTGOMERY = "montgomery"
    PSEUDO_MERSENNE = "pseudo_mersenne"


@dataclass(frozen=True)
class PseudoMersenne:
    """
    A prime written as ``2**k - c`` with ``0 < c < 2**(k // 2)``.

    The bound on ``c`` is what makes :meth:`reduce` terminate quickly: it is
    the condition under which folding the high half back in shrinks the value,
    so the fold can be iterated to a fixed point instead of diverging.
    """

    k: int
    c: int

    @property
    def prime(self) -> int:
        """The modulus itself. Not constant time."""
        return (1 << self.k) - self.c

    def reduce(self, x: int) -> int:
        """
        Reduce ``x >= 0`` modulo ``2**k - c`` by shift-and-add, no division.

        Uses ``2**k == c (mod p)``: split ``x`` at bit ``k`` into ``lo + 2**k *
        hi`` and replace it with ``lo + c*hi``, which is congruent and strictly
        smaller whenever ``hi > 0`` and ``c < 2**k``. Iterating reaches a value
        below ``2**k``, after which at most a few subtractions of ``p`` finish
        the job.

        Rejects negative input rather than guessing a sign convention.
        Not constant time: the loop count depends on ``x``.
        """
        if x < 0:
            raise ValueError(f"reduce expects a non-negative value, got {x}")
        p = self.prime
        mask = (1 << self.k) - 1
        while x > mask:
            x = (x & mask) + self.c * (x >> self.k)
        while x >= p:
            x -= p
        return x


def as_pseudo_mersenne(p: int) -> PseudoMersenne | None:
    """
    Describe ``p`` as ``2**k - c`` if it has that shape, else return ``None``.

    ``k`` is ``p.bit_length()``, so ``c = 2**k - p`` is positive for every
    ``p`` that is not itself a power of two. The shape is only *useful* when
    ``c`` is small, so the bound ``c < 2**(k // 2)`` is enforced here rather
    than left to the caller: a modulus that fails it gets ``None`` and the
    caller falls back to Barrett or Montgomery.

    Not constant time.
    """
    if p < 3:
        return None
    k = p.bit_length()
    c = (1 << k) - p
    if c <= 0 or c >= (1 << (k // 2)):
        return None
    return PseudoMersenne(k=k, c=c)


class PrimeField:
    """
    GF(p) for an odd ``p``, with a selectable reduction strategy.

    Primality of ``p`` is a *precondition*, not something this class verifies:
    the primality test is a separate registry entry
    (``primality-testing-bpsw``) with its own owning module, and duplicating a
    weaker copy of it here would be exactly the unregistered implementation
    the registry exists to prevent. What is checked is the structure this
    module's own arithmetic depends on -- ``p`` odd and at least 3, which is
    what makes ``R`` invertible for Montgomery form.

    Elements are plain ``int`` in ``[0, p)``, in ordinary representation, for
    every method except the ``mont_*`` family. ``mul`` converts in and out of
    Montgomery form when that strategy is selected, so the strategy is not
    observable in the results -- only in the cost.

    No method is constant time; see the module docstring.
    """

    __slots__ = (
        "_barrett_mu",
        "_barrett_shift_hi",
        "_barrett_shift_lo",
        "_mont_bits",
        "_mont_mask",
        "_mont_n_prime",
        "_mont_r2",
        "_pseudo_mersenne",
        "_reduction",
        "p",
    )

    def __init__(self, p: int, reduction: Reduction = Reduction.PYTHON) -> None:
        if not isinstance(p, int) or isinstance(p, bool):
            raise TypeError(f"modulus must be an int, got {type(p).__name__}")
        if p < 3:
            raise ValueError(f"modulus must be an odd prime >= 3, got {p}")
        if p % 2 == 0:
            raise ValueError(f"modulus must be odd, got {p}")
        if not isinstance(reduction, Reduction):
            raise TypeError(
                f"reduction must be a Reduction, got {type(reduction).__name__}"
            )

        self.p = p
        self._reduction = reduction
        self._pseudo_mersenne = as_pseudo_mersenne(p)
        if reduction is Reduction.PSEUDO_MERSENNE and self._pseudo_mersenne is None:
            raise ValueError(
                f"modulus {p} is not of the form 2**k - c with c < 2**(k // 2); "
                "pseudo-Mersenne reduction does not apply to it"
            )

        k = p.bit_length()
        self._barrett_shift_lo = k - 1
        self._barrett_shift_hi = k + 1
        self._barrett_mu = (1 << (2 * k)) // p

        self._mont_bits = ((k + 63) // 64) * 64
        self._mont_mask = (1 << self._mont_bits) - 1
        r = 1 << self._mont_bits
        self._mont_n_prime = (-pow(p, -1, r)) % r
        self._mont_r2 = (r * r) % p

    # ---- introspection -------------------------------------------------

    @property
    def reduction(self) -> Reduction:
        """The configured strategy. Not constant time (attribute read)."""
        return self._reduction

    @property
    def pseudo_mersenne(self) -> PseudoMersenne | None:
        """The ``2**k - c`` shape of the modulus, or ``None``. Not constant time."""
        return self._pseudo_mersenne

    def __repr__(self) -> str:
        return f"PrimeField(p={self.p}, reduction={self._reduction.value})"

    # ---- reduction -----------------------------------------------------

    def reduce(self, x: int) -> int:
        """
        Reduce any integer, of any sign, into ``[0, p)``.

        Dispatches on the configured strategy for values that strategy's own
        bound covers, and falls back to ``%`` otherwise: Barrett is proven only
        for ``0 <= x < 2**(2k)`` and the fold only for ``x >= 0``, and silently
        applying either past its bound is the carry-edge bug the registry
        warns about. The result is the same residue whichever branch runs --
        the strategy changes the cost, never the value. Not constant time.
        """
        if x < 0:
            return x % self.p
        pm = self._pseudo_mersenne
        if self._reduction is Reduction.PSEUDO_MERSENNE and pm is not None:
            return pm.reduce(x)
        if self._reduction is Reduction.BARRETT and x < (1 << (2 * self.p.bit_length())):
            return self._barrett_reduce(x)
        return x % self.p

    def _barrett_reduce(self, x: int) -> int:
        """
        HAC 14.42 with base 2, for ``0 <= x < 2**(2k)``.

        The estimate ``q3`` is never more than 2 too small, so the corrective
        loop runs at most twice; it is written as a loop rather than two
        unrolled subtractions so that a violated precondition still yields the
        right answer instead of a wrong one. Not constant time.
        """
        q = ((x >> self._barrett_shift_lo) * self._barrett_mu) >> self._barrett_shift_hi
        r = x - q * self.p
        while r >= self.p:
            r -= self.p
        return r

    # ---- ordinary arithmetic -------------------------------------------

    def add(self, a: int, b: int) -> int:
        """Sum in GF(p). Inputs are reduced first. Not constant time."""
        return (self.reduce(a) + self.reduce(b)) % self.p

    def sub(self, a: int, b: int) -> int:
        """Difference in GF(p). Not constant time."""
        return (self.reduce(a) - self.reduce(b)) % self.p

    def neg(self, a: int) -> int:
        """Additive inverse in GF(p). Not constant time."""
        return (-self.reduce(a)) % self.p

    def mul(self, a: int, b: int) -> int:
        """
        Product in GF(p), via the configured strategy.

        Montgomery form is entered and left inside this call, so the result is
        an ordinary residue whichever strategy is set. Callers doing many
        multiplications in a row should stay in Montgomery form themselves with
        :meth:`to_montgomery` / :meth:`mont_mul` / :meth:`from_montgomery`
        instead of paying the conversion twice per product. Not constant time.
        """
        a = self.reduce(a)
        b = self.reduce(b)
        if self._reduction is Reduction.MONTGOMERY:
            return self.from_montgomery(
                self.mont_mul(self.to_montgomery(a), self.to_montgomery(b))
            )
        return self.reduce(a * b)

    def square(self, a: int) -> int:
        """``a**2`` in GF(p). Not constant time."""
        return self.mul(a, a)

    def pow(self, a: int, e: int) -> int:
        """
        ``a**e`` in GF(p), with negative ``e`` inverting first.

        Delegates to CPython's ``pow``, which is a windowed square-and-multiply
        and **leaks the exponent through timing**. Never raise a secret scalar
        with this. Not constant time.
        """
        a = self.reduce(a)
        if e < 0:
            return pow(self.inv(a), -e, self.p)
        return pow(a, e, self.p)

    def inv(self, a: int) -> int:
        """
        Multiplicative inverse of ``a``, which must not be ``0 mod p``.

        Raises :class:`ZeroDivisionError` for a non-invertible element rather
        than returning a plausible-looking value, because a silent zero here
        surfaces later as a point at infinity or a forged-looking signature.
        Not constant time.
        """
        a = self.reduce(a)
        if a == 0:
            raise ZeroDivisionError("0 has no multiplicative inverse in GF(p)")
        return pow(a, -1, self.p)

    def div(self, a: int, b: int) -> int:
        """``a * b**-1`` in GF(p); raises if ``b`` is zero. Not constant time."""
        return self.mul(a, self.inv(b))

    # ---- Montgomery form -----------------------------------------------

    @property
    def montgomery_bits(self) -> int:
        """Width of the Montgomery radix ``R = 2**bits``. Not constant time."""
        return self._mont_bits

    def to_montgomery(self, a: int) -> int:
        """Map ``a`` to ``a*R mod p``. Not constant time."""
        return self.mont_mul(self.reduce(a), self._mont_r2)

    def from_montgomery(self, a: int) -> int:
        """Map ``a*R mod p`` back to ``a``. Not constant time."""
        return self.mont_reduce(a)

    def mont_reduce(self, t: int) -> int:
        """
        Montgomery reduction: ``t * R**-1 mod p`` for ``0 <= t < p*R``.

        The precondition is enforced, not assumed. Outside it the final
        conditional subtraction is not enough to bring the result below ``p``,
        and the function would return a value that is congruent but not
        reduced -- which compares unequal to the correct residue and turns into
        a wrong answer several layers away from here. Not constant time.
        """
        if not 0 <= t < self.p << self._mont_bits:
            raise ValueError(
                f"mont_reduce expects 0 <= t < p*R (R = 2**{self._mont_bits}), got {t}"
            )
        m = ((t & self._mont_mask) * self._mont_n_prime) & self._mont_mask
        u = (t + m * self.p) >> self._mont_bits
        if u >= self.p:
            u -= self.p
        return u

    def mont_mul(self, a: int, b: int) -> int:
        """
        Product of two Montgomery-form values, result in Montgomery form.

        Both operands must already be reduced into ``[0, p)``; their product is
        then below ``p*R`` and :meth:`mont_reduce` applies. Not constant time.
        """
        if not 0 <= a < self.p or not 0 <= b < self.p:
            raise ValueError("mont_mul expects both operands reduced into [0, p)")
        return self.mont_reduce(a * b)
