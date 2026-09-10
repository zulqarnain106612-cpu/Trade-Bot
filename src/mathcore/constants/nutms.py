"""
Nothing-up-my-sleeve constants, re-derived rather than trusted.

Owns the ``nothing-up-my-sleeve-constants`` registry entry.

Cryptographic primitives are full of magic-looking hex: SHA-256's round
constants, SHA-1's, MD5's table, Blowfish's S-boxes, the TEA/RC5 delta
``0x9E3779B9``. Each *looks* exactly like the arbitrary numerology the folklore
gate rejects -- and the only thing separating the two is that these have a
public derivation from an irrational nobody could have rigged: cube roots of
small primes, square roots, sines, digits of pi, the golden ratio. A constant
with such a derivation is "nothing up my sleeve"; one without is a place to
hide a backdoor.

This module re-derives each set from its stated irrational and checks it
matches the published value. That is the whole point: it converts "these
digits are pi, trust me" into "these digits are pi, here is the computation".
:func:`verify_published_constants` runs every set and is the honest mirror of
:mod:`src.mathcore.folklore` -- one refuses numbers with no derivation, this
confirms the derivation of the numbers that have one.

High-precision arithmetic uses :mod:`decimal`; results are public and
deterministic, so constant-time does not apply.

References: FIPS 180-4 (SHA-1, SHA-256); RFC 1321 (MD5); Schneier's Blowfish
paper; Wheeler & Needham, *TEA* (1994); RFC 2104.
"""

from __future__ import annotations

from decimal import Decimal, getcontext

__all__ = [
    "blowfish_pi_words",
    "md5_sine_table",
    "sha1_round_constants",
    "sha256_round_constants",
    "tea_delta",
    "verify_published_constants",
]

# Enough guard digits that truncating to 32-bit words is never wrong.
getcontext().prec = 120


def _first_primes(count: int) -> list[int]:
    """The first ``count`` primes, by trial division. Not constant time."""
    primes: list[int] = []
    candidate = 2
    while len(primes) < count:
        if all(candidate % p for p in primes if p * p <= candidate):
            primes.append(candidate)
        candidate += 1
    return primes


def _frac_bits(value: Decimal, bits: int) -> int:
    """The first ``bits`` bits of the fractional part of ``value``."""
    frac = value - int(value)
    return int(frac * (Decimal(2) ** bits))


def sha256_round_constants(count: int = 64) -> list[int]:
    """
    SHA-256's K constants: the first 32 bits of the fractional parts of the
    cube roots of the first ``count`` primes (FIPS 180-4).
    """
    out = []
    for p in _first_primes(count):
        root = Decimal(p) ** (Decimal(1) / Decimal(3))
        out.append(_frac_bits(root, 32))
    return out


def sha1_round_constants() -> list[int]:
    """
    SHA-1's four constants: ``floor(2^30 * sqrt(d))`` for ``d`` in 2, 3, 5, 10.

    Note these keep the integer part -- ``sqrt(2) * 2^30`` exceeds ``2^30`` --
    unlike the SHA-256 constants, which are fractional. Getting that right is
    the whole reason to re-derive rather than eyeball.
    """
    return [int(Decimal(d).sqrt() * (Decimal(2) ** 30)) for d in (2, 3, 5, 10)]


def md5_sine_table() -> list[int]:
    """
    MD5's T table: ``floor(2^32 * abs(sin(i)))`` for ``i`` in 1..64 (RFC 1321),
    with ``i`` in radians.
    """
    out = []
    for i in range(1, 65):
        # Decimal has no sin; math.sin is exact enough here because the table
        # is defined by IEEE double sin in the reference and the 2^32 scaling
        # leaves ample margin. Re-derivation, not bit-exact transcendental.
        import math

        out.append(int(abs(math.sin(i)) * (2**32)))
    return out


def blowfish_pi_words(count: int = 18) -> list[int]:
    """
    Blowfish's P-array seed: successive 32-bit words of the fractional part of
    pi. ``count`` defaults to 18, the P-array length; the S-boxes continue the
    same stream.
    """
    pi = _pi()
    frac = pi - int(pi)
    words = []
    scaled = frac
    for _ in range(count):
        scaled *= Decimal(2) ** 32
        words.append(int(scaled))
        scaled -= int(scaled)
    return words


def tea_delta() -> int:
    """
    The TEA/RC5 delta ``0x9E3779B9``: ``floor(2^32 * (sqrt(5) - 1) / 2)``, the
    fractional part of the golden ratio scaled to 32 bits.
    """
    phi_frac = (Decimal(5).sqrt() - 1) / 2
    return int(phi_frac * (Decimal(2) ** 32))


def _pi() -> Decimal:
    """Pi to the current decimal precision, by Machin's formula."""

    def arctan_inv(x: int) -> Decimal:
        # arctan(1/x) = sum (-1)^k / ((2k+1) x^(2k+1))
        total = Decimal(0)
        term = Decimal(1) / Decimal(x)
        x2 = Decimal(x) * Decimal(x)
        k = 0
        while True:
            increment = term / (2 * k + 1)
            if increment == 0:
                break
            total += increment if k % 2 == 0 else -increment
            term /= x2
            k += 1
        return total

    return 16 * arctan_inv(5) - 4 * arctan_inv(239)


# The published values, as the derivations must reproduce them.
_PUBLISHED = {
    "sha256": (
        sha256_round_constants,
        (),
        [0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5],
    ),
    "sha1": (
        sha1_round_constants,
        (),
        [0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6],
    ),
    "md5": (md5_sine_table, (), [0xD76AA478, 0xE8C7B756, 0x242070DB]),
    "blowfish": (
        blowfish_pi_words,
        (),
        [0x243F6A88, 0x85A308D3, 0x13198A2E],
    ),
    "tea": (tea_delta, (), 0x9E3779B9),
}


def verify_published_constants() -> dict[str, bool]:
    """
    Re-derive every constant set and confirm it matches its published prefix.

    Returns ``{name: True}`` for each set, and raises :class:`ValueError` the
    moment any derivation fails to reproduce the standard -- a mismatch means
    either the derivation is wrong or the "constant" was never what it claimed,
    and both must stop the build. The covered sets are SHA-256 (cube roots),
    SHA-1 (square roots), MD5 (sines), Blowfish (pi digits) and the TEA/RC5
    golden-ratio delta. Not constant time.
    """
    results: dict[str, bool] = {}
    for name, (func, args, expected) in _PUBLISHED.items():
        derived = func(*args)
        if isinstance(expected, int):
            ok = derived == expected
        else:
            ok = derived[: len(expected)] == expected
        if not ok:
            raise ValueError(
                f"{name}: re-derivation does not match the published constant. "
                "Either the derivation is wrong or the constant is not what it "
                f"claims to be. Derived {derived!r}, expected prefix {expected!r}."
            )
        results[name] = True
    return results
