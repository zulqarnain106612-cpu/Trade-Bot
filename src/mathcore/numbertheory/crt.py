"""
The Chinese Remainder Theorem, RSA-CRT recombination, and the fault check that
must accompany it.

Owns the ``chinese-remainder-theorem`` registry entry.

CRT splits a private RSA operation into two half-size operations modulo ``p``
and ``q``, roughly a fourfold speedup. That speedup comes with the sharpest
failure mode in this whole package: **a single wrong bit in one of the two
halves lets an observer factor the modulus from the resulting signature with
one GCD.** The fault does not have to be injected by an attacker with a laser
-- bad RAM produces it too, and the consequence is identical.

So this module ships :func:`detect_crt_fault` next to :func:`rsa_crt_recombine`
rather than in a separate "hardening" layer. A CRT signature that has not been
verified against the public exponent before release is not a signature, it is
a private key with extra steps.

**Constant time: none of it.** The extended Euclidean algorithm branches on its
inputs and CPython integers branch on width. Nothing here may gate a secret.
Every public function repeats this.

References: HAC 2.4.3 and 14.5; Boneh, DeMillo & Lipton, *On the Importance of
Checking Cryptographic Protocols for Faults*, EUROCRYPT '97; Garner (1959).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "crt",
    "crt_pair",
    "detect_crt_fault",
    "rsa_crt_recombine",
]


def crt_pair(a1: int, n1: int, a2: int, n2: int) -> tuple[int, int]:
    """
    Solve ``x == a1 (mod n1)`` and ``x == a2 (mod n2)`` for coprime moduli.

    Returns ``(x, n1 * n2)`` with ``0 <= x < n1 * n2``.

    Coprimality is checked, not assumed. For non-coprime moduli a solution
    exists only when the residues agree modulo ``gcd(n1, n2)``, and the
    combined modulus is the lcm rather than the product -- a different theorem
    with a different answer. Silently returning something for that case would
    hand back a value satisfying neither congruence. Not constant time.
    """
    _check_modulus(n1, "n1")
    _check_modulus(n2, "n2")
    g = math.gcd(n1, n2)
    if g != 1:
        raise ValueError(
            f"crt_pair requires coprime moduli; gcd({n1}, {n2}) = {g}. "
            "Non-coprime moduli need a compatibility check and give the lcm, "
            "not the product"
        )
    modulus = n1 * n2
    # x = a1 + n1 * ((a2 - a1) * n1^-1 mod n2), which stays within one
    # multiplication of the final size rather than summing full-width terms.
    correction = ((a2 - a1) * pow(n1, -1, n2)) % n2
    return (a1 + n1 * correction) % modulus, modulus


def crt(residues: Sequence[int], moduli: Sequence[int]) -> tuple[int, int]:
    """
    Solve a system of congruences with pairwise coprime moduli.

    Returns ``(x, prod(moduli))`` with ``0 <= x < prod(moduli)``. An empty
    system returns ``(0, 1)``: every integer satisfies no constraints, and
    ``0 mod 1`` is the canonical representative of that. Raising instead would
    make ``crt`` fail on the base case of any recursion that uses it.

    Folds :func:`crt_pair` left to right, so pairwise coprimality is enforced
    incrementally -- a repeated or sharing modulus is caught at the pair that
    introduces it, and the error names it. Not constant time.
    """
    if len(residues) != len(moduli):
        raise ValueError(
            f"crt needs one residue per modulus, got {len(residues)} and {len(moduli)}"
        )
    x, modulus = 0, 1
    for a, n in zip(residues, moduli, strict=True):
        x, modulus = crt_pair(x, modulus, a, n)
    return x, modulus


def rsa_crt_recombine(m1: int, m2: int, p: int, q: int, q_inv: int | None = None) -> int:
    """
    Garner recombination: the ``x`` with ``x == m1 (mod p)`` and ``x == m2 (mod q)``.

    This is :func:`crt_pair` specialised to the shape RSA uses, and named for
    it so that call sites read as what they are. ``q_inv`` is ``q**-1 mod p``,
    which a private key stores precomputed; passing it skips the inversion, and
    a wrong one is rejected rather than used -- an unchecked ``q_inv`` produces
    a value congruent to neither half, which is exactly the faulty output the
    rest of this module exists to catch.

    Not constant time. **The result must pass :func:`detect_crt_fault` before
    it is released to anyone.**
    """
    _check_modulus(p, "p")
    _check_modulus(q, "q")
    if math.gcd(p, q) != 1:
        raise ValueError(f"rsa_crt_recombine requires coprime p and q, got {p} and {q}")
    if q_inv is None:
        q_inv = pow(q, -1, p)
    elif (q * q_inv) % p != 1:
        raise ValueError("q_inv is not the inverse of q modulo p")
    h = (q_inv * (m1 - m2)) % p
    return (m2 + h * q) % (p * q)


def detect_crt_fault(
    *,
    signature: int,
    message: int,
    public_exponent: int,
    modulus: int,
) -> int | None:
    """
    Check a CRT signature, and recover a factor of ``modulus`` if it is faulty.

    Returns ``None`` when the signature verifies -- the only case in which it
    may be released. Otherwise returns a non-trivial factor of ``modulus`` when
    one falls out, or raises when the signature is wrong for some other reason.

    The mechanism is the Boneh-DeMillo-Lipton observation: if exactly one of
    the two CRT halves was computed wrongly, then ``signature**e - message`` is
    divisible by one prime factor and not the other, so its GCD with the
    modulus *is* that factor. This function is the attack, run by the defender,
    on their own output, before anyone else can run it.

    The return value is deliberately awkward to ignore. A boolean "valid"
    would let a caller treat a fault as an ordinary verification failure and
    retry; a returned factor cannot be read as anything but "this key is now
    public, destroy it".

    Callers must treat *any* non-``None`` result as total compromise of the
    key pair, not as a transient error to retry. Not constant time.
    """
    _check_modulus(modulus, "modulus")
    if public_exponent < 1:
        raise ValueError(f"public exponent must be >= 1, got {public_exponent}")

    recovered = pow(signature, public_exponent, modulus)
    if recovered == message % modulus:
        return None

    factor = math.gcd(recovered - message, modulus)
    if 1 < factor < modulus:
        return factor
    raise ValueError(
        "the signature does not verify and no factor falls out of the GCD; the "
        "message, exponent or modulus does not belong to this signature, which "
        "is a different bug from a CRT fault"
    )


def _check_modulus(n: int, name: str) -> None:
    """Shared argument validation. Not constant time."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"{name} must be an int, got {type(n).__name__}")
    if n < 1:
        raise ValueError(f"{name} must be >= 1, got {n}")
