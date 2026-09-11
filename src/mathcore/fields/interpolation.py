"""
Lagrange interpolation and Shamir secret sharing over a prime field.

Owns the ``lagrange-interpolation`` registry entry.

A polynomial of degree ``t`` is fixed by ``t+1`` points and by no fewer -- and
that exact-threshold property is the whole of Shamir's secret sharing and of
threshold signing. Hide a secret as the constant term of a random degree-``t``
polynomial, hand out points on it, and any ``t+1`` holders reconstruct the
secret by interpolating back to ``x = 0`` while any ``t`` learn nothing.

This module works over a prime field via
:class:`~src.mathcore.fields.prime_field.PrimeField`, so the arithmetic is the
project's own rather than a private modular routine. Sharing a *secret* means
the split step consumes randomness the caller supplies -- the module never
invents its own, so a caller cannot accidentally get a non-random,
reconstructible-by-anyone sharing.

**Not constant time.** The interpolation and the field are variable-time, so
this is a correctness reference for the scheme, not a production secret-sharing
implementation that must resist timing analysis on the shares.

References: Shamir, *How to share a secret* (1979); any text on Lagrange
interpolation; the threshold-signature literature (FROST, GG20) for the signing
use.
"""

from __future__ import annotations

from collections.abc import Sequence

from .prime_field import PrimeField

__all__ = [
    "lagrange_interpolate_at",
    "shamir_reconstruct",
    "shamir_split",
]


def lagrange_interpolate_at(points: Sequence[tuple[int, int]], x: int, field: PrimeField) -> int:
    """
    Evaluate, at ``x``, the unique polynomial through ``points`` over ``field``.

    ``points`` are ``(x_i, y_i)`` pairs with distinct ``x_i``; the result is the
    interpolating polynomial's value at ``x`` without ever forming the
    polynomial's coefficients, since a single evaluation (reconstruction at
    ``x = 0``) is all secret sharing needs. Duplicate ``x_i`` are rejected --
    they either contradict each other or carry no new information, and both make
    the Lagrange denominator zero. Not constant time.
    """
    xs = [p[0] % field.p for p in points]
    if len(set(xs)) != len(xs):
        raise ValueError("interpolation points must have distinct x coordinates")
    if not points:
        raise ValueError("need at least one point to interpolate")

    total = 0
    for i, (xi, yi) in enumerate(points):
        numerator = 1
        denominator = 1
        for j, (xj, _yj) in enumerate(points):
            if i == j:
                continue
            numerator = field.mul(numerator, field.sub(x, xj))
            denominator = field.mul(denominator, field.sub(xi, xj))
        term = field.mul(yi % field.p, field.div(numerator, denominator))
        total = field.add(total, term)
    return total


def shamir_split(
    secret: int,
    threshold: int,
    coefficients: Sequence[int],
    share_xs: Sequence[int],
    field: PrimeField,
) -> list[tuple[int, int]]:
    """
    Split ``secret`` into shares on a degree-``threshold - 1`` polynomial.

    The polynomial is ``secret + c_1 x + ... + c_{t-1} x^{t-1}`` with the
    ``coefficients`` supplied by the caller -- these are the secret randomness
    of the scheme, and requiring them rather than sampling internally is
    deliberate: it keeps this module free of a random source and forces the
    caller to own the entropy, whose quality is the whole security of the
    sharing. There must be exactly ``threshold - 1`` of them.

    Returns one ``(x, share)`` per ``x`` in ``share_xs``; the ``x`` must be
    non-zero (``x = 0`` is the secret itself) and distinct. Reconstruction needs
    any ``threshold`` of the returned shares. Not constant time.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be at least 1, got {threshold}")
    if len(coefficients) != threshold - 1:
        raise ValueError(
            f"need exactly {threshold - 1} coefficients for threshold {threshold}, "
            f"got {len(coefficients)}"
        )
    if any(x % field.p == 0 for x in share_xs):
        raise ValueError("share x coordinates must be non-zero; x = 0 is the secret")
    if len({x % field.p for x in share_xs}) != len(share_xs):
        raise ValueError("share x coordinates must be distinct")

    poly = [secret % field.p, *[c % field.p for c in coefficients]]
    shares = []
    for x in share_xs:
        value = 0
        for coeff in reversed(poly):
            value = field.add(field.mul(value, x), coeff)
        shares.append((x % field.p, value))
    return shares


def shamir_reconstruct(shares: Sequence[tuple[int, int]], field: PrimeField) -> int:
    """
    Recover the secret from ``threshold`` or more shares.

    The secret is the sharing polynomial's constant term, so this is
    :func:`lagrange_interpolate_at` evaluated at ``x = 0``. Fewer than the
    threshold shares interpolate a *different* polynomial and yield a wrong
    value -- the scheme gives no error for too few shares, which is exactly its
    secrecy property, so the caller is responsible for supplying enough. Not
    constant time.
    """
    if not shares:
        raise ValueError("need at least one share to reconstruct")
    return lagrange_interpolate_at(shares, 0, field)
