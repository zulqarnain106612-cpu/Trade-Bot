"""
Security-level bounds for the discrete logarithm and for factorisation.

Owns two registry entries:

* ``generic-dlp-algorithms`` -- the generic square-root attacks (baby-step
  giant-step, Pollard rho for logarithms) that fix a group's real security at
  half its bit length, and Pohlig-Hellman, which drops it to the largest prime
  factor of the group order. This is why a 256-bit prime-order group gives
  128-bit security and a group with smooth order gives almost none.
* ``number-field-sieve`` -- the subexponential ``L_n[1/3]`` cost of factoring,
  the entire reason RSA needs 2048-3072 bits where an elliptic curve needs 256.

These are *estimators*, not attacks: they turn a group order or modulus size
into the bits of work an attacker faces, so a caller can size a parameter
honestly instead of by folklore. Nothing here runs an attack, so there is no
secret and constant-time does not apply.

References: Pohlig & Hellman (1978); Shanks' baby-step giant-step; Pollard's rho
for logarithms; the general number field sieve complexity ``L_n[1/3, (64/9)^(1/3)]``.
"""

from __future__ import annotations

import math

__all__ = [
    "generic_dlp_security_bits",
    "nfs_cost_bits",
    "pohlig_hellman_security_bits",
]


def _largest_prime_factor(n: int) -> int:
    """The largest prime factor of ``n``, by trial division. Small ``n`` only."""
    largest = 1
    d = 2
    remaining = n
    while d * d <= remaining:
        while remaining % d == 0:
            largest = d
            remaining //= d
        d += 1
    return max(largest, remaining)


def generic_dlp_security_bits(group_order: int) -> float:
    """
    The security in bits against a generic square-root attack on a group of
    order ``group_order``.

    A generic algorithm costs about ``sqrt(group_order)`` operations, so the
    security is ``log2(group_order) / 2`` -- half the bit length. This is the
    ceiling; :func:`pohlig_hellman_security_bits` gives the true figure once the
    order's factorisation is accounted for. Not constant time.
    """
    if group_order < 2:
        raise ValueError(f"group_order must be at least 2, got {group_order}")
    return math.log2(group_order) / 2


def pohlig_hellman_security_bits(group_order: int) -> float:
    """
    The security in bits after Pohlig-Hellman reduces the problem to the group's
    largest prime-order subgroup.

    Pohlig-Hellman solves a DLP in a group of order ``prod p_i^{e_i}`` by
    solving it in each ``p_i``-subgroup and recombining, so the cost is set by
    the *largest* prime factor, not the whole order. The security is therefore
    ``log2(largest_prime_factor) / 2``. A group whose order is smooth -- all
    small prime factors -- has almost no security however large it is, which is
    why prime-order groups are mandated. Uses trial-division factoring, so it is
    for modest orders and analysis, not 256-bit production orders. Not constant
    time.
    """
    if group_order < 2:
        raise ValueError(f"group_order must be at least 2, got {group_order}")
    return math.log2(_largest_prime_factor(group_order)) / 2


def nfs_cost_bits(modulus_bits: int) -> float:
    """
    The estimated cost in bits of factoring an ``modulus_bits``-bit RSA modulus
    with the general number field sieve.

    Evaluates ``L_n[1/3, c]`` with ``c = (64/9)^(1/3)`` and converts the
    operation count to bits (``log2``). This is the curve that makes RSA-2048
    give roughly 112-bit security and RSA-3072 roughly 128-bit, matching NIST
    SP 800-57 -- and it is why RSA keys dwarf elliptic-curve keys at the same
    security. An estimate of asymptotic cost, not a promise about a specific
    modulus. Not constant time.
    """
    if modulus_bits < 2:
        raise ValueError(f"modulus_bits must be at least 2, got {modulus_bits}")
    n = modulus_bits * math.log(2)  # ln of the modulus
    c = (64 / 9) ** (1 / 3)
    ln_n = n
    ln_ln_n = math.log(ln_n)
    ops_ln = c * (ln_n ** (1 / 3)) * (ln_ln_n ** (2 / 3))
    return ops_ln / math.log(2)
