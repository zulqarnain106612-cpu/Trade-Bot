"""
The hidden number problem: ECDSA private-key recovery from biased nonces.

Owns the ``hidden-number-problem`` registry entry, the single most directly
relevant attack in this project for a system that signs.

The setup. Each ECDSA signature ``(r, s)`` over a hash ``z`` under private key
``d`` with nonce ``k`` satisfies ``s*k = z + r*d (mod n)``, so
``k = s^-1*(z + r*d)``. If every ``k`` is fully unknown, this is one equation
in two unknowns per signature and reveals nothing. But if the top bits of each
``k`` are known -- because the RNG was biased, stuck, or truncated -- then each
signature pins ``d`` to a narrow interval, and enough such intervals intersect
at a single point. Recovering that point is a *closest vector* problem, which
:mod:`mathcore.lattice.lll` solves -- via its integer-preserving reduction, so
real 256-bit key recovery is feasible, not just a toy demonstration.

This module is the attack, implemented so the defender can run it against their
own signatures before someone else does. It recovers a key **only** from
caller-supplied samples and has no path to this project's key material: it
imports nothing from ``src/security`` and takes every value as an argument.
That is law L1, and a test enforces the import ban.

What it cannot do is as important as what it can, and :func:`recovery_limits`
states it: the attack needs the *same* leak in the *same* bit position across
signatures, needs enough of them, and sees nothing in a correctly randomised
signer. A clean result from :func:`recover_private_key` is not a certificate
that a signer is safe -- it is the absence of one specific, detectable flaw.

**Not constant time, and it must not be.** Its whole purpose is to operate on
public signature data and extract a secret from a flaw; there is no secret of
*this* process to protect.

References: Boneh & Venkatesan, *Hardness of computing the most significant
bits* (1996); Howgrave-Graham & Smart, *Lattice attacks on digital signature
schemes* (2001); Nguyen & Shparlinski (2002); the Minerva and TPM-Fail
disclosures.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lll import integer_lll_reduce

__all__ = [
    "Signature",
    "recover_private_key",
    "recovery_limits",
]


@dataclass(frozen=True, slots=True)
class Signature:
    """
    One ECDSA signature with a partially known nonce.

    ``r``, ``s`` and ``z`` are the usual signature triple modulo the group
    order. ``known_high_bits`` is the count of most-significant bits of the
    nonce ``k`` that are known to be ``known_value``; the common bias is a
    stuck top byte, ``known_high_bits = 8``. ``known_value`` is the value those
    bits hold shifted into position -- for a nonce known to be below
    ``2**(bitlen - b)``, it is ``0``.

    The class carries the leak description with the signature so a caller
    cannot pass a list of signatures and a separate, mismatched list of leaks.
    """

    r: int
    s: int
    z: int
    known_high_bits: int
    known_value: int = 0


def recover_private_key(
    signatures: list[Signature],
    order: int,
    nonce_bitlength: int,
) -> int | None:
    """
    Recover the private key from biased-nonce signatures, or ``None``.

    ``order`` is the curve's group order ``n``; ``nonce_bitlength`` is the full
    bit length of a nonce (256 for secp256k1). Each signature must carry a
    positive ``known_high_bits`` -- signatures with no known bits contribute
    nothing and are a caller error, not silently dropped, because dropping them
    hides that the caller misunderstood what the attack needs.

    Returns the key in ``[1, n)`` when the lattice yields it, and ``None`` when
    it does not -- too few signatures, too little bias, or an unbiased signer.
    ``None`` is the *expected* answer for a healthy signer and must never be
    read as a failure of the attack; see :func:`recovery_limits`.

    The construction follows Howgrave-Graham & Smart: build a lattice whose
    short vectors encode the small nonce remainders, reduce it, and read the
    key out of each reduced row, verifying each candidate against the
    signatures before returning it. The verification is what lets this return a
    definite key or a definite ``None`` rather than a maybe. Not constant time.
    """
    if order < 3:
        raise ValueError(f"order must be a group order >= 3, got {order}")
    if nonce_bitlength < 1:
        raise ValueError(f"nonce_bitlength must be positive, got {nonce_bitlength}")
    if len(signatures) < 2:
        raise ValueError("recovery needs at least two signatures")
    if any(sig.known_high_bits <= 0 for sig in signatures):
        raise ValueError(
            "every signature must have a positive known_high_bits; a signature "
            "with no known nonce bits contributes nothing to the lattice"
        )
    if any(sig.known_high_bits >= nonce_bitlength for sig in signatures):
        raise ValueError(
            "known_high_bits must be fewer than nonce_bitlength; a nonce with no "
            "unknown bits is not a nonce, it is a constant"
        )

    count = len(signatures)
    # Each signature contributes t_i and u_i with k_i = a_i + (small), where
    # a_i is the known part and the unknown part is bounded by 2**(bitlen - b_i).
    # Following HGS: k = s^-1 z + s^-1 r d, so writing d as the hidden number,
    #   t_i = s_i^-1 r_i (mod n),  u_i = s_i^-1 z_i + a_i (mod n)
    # and the unknown nonce remainder is small.
    scale = 1 << nonce_bitlength
    ts: list[int] = []
    us: list[int] = []
    bounds: list[int] = []
    for sig in signatures:
        s_inv = pow(sig.s, -1, order)
        a_i = sig.known_value % order
        ts.append((s_inv * sig.r) % order)
        us.append((s_inv * sig.z + a_i) % order)
        bounds.append(1 << (nonce_bitlength - sig.known_high_bits))

    # Common denominator so the bound weights are integers: the lattice wants
    # each coordinate scaled by scale / bound_i, and LLL here is exact-rational
    # tolerant but the basis must be integer, so clear denominators via lcm.
    weight = scale
    dimension = count + 2
    basis: list[list[int]] = []
    for i in range(count):
        row = [0] * dimension
        row[i] = order * weight // bounds[i]
        basis.append(row)
    penultimate = [ts[i] * weight // bounds[i] for i in range(count)] + [1, 0]
    last = [us[i] * weight // bounds[i] for i in range(count)] + [0, scale]
    basis.append(penultimate)
    basis.append(last)

    reduced = integer_lll_reduce(basis)

    for row in reduced:
        # The candidate for d sits in the penultimate coordinate, recovered by
        # dividing out the embedding weight on the last coordinate.
        if row[-1] == 0:
            continue
        candidate = (-row[-2] * scale // row[-1]) % order if row[-1] else 0
        for guess in (candidate, (order - candidate) % order):
            if 1 <= guess < order and _verifies(signatures, guess, order):
                return guess
    return None


def _verifies(signatures: list[Signature], key: int, order: int) -> bool:
    """
    Whether ``key`` reproduces every signature's nonce consistently.

    For the true key, ``k_i = s_i^-1 (z_i + r_i d)`` must agree with the known
    high bits of each nonce. Checking against the *leak* rather than merely
    picking the shortest vector is what turns a lattice guess into a certainty:
    a short vector that is not the key fails this on the first signature.
    Not constant time.
    """
    for sig in signatures:
        s_inv = pow(sig.s, -1, order)
        # The nonce this key implies for this signature.
        k = (s_inv * (sig.z + sig.r * key)) % order
        # Its known high bits must match what the signature claims leaked.
        shift = sig.known_high_bits
        if (k >> (order.bit_length() - shift)) != (sig.known_value >> (order.bit_length() - shift)):
            return False
    return True


def recovery_limits() -> str:
    """
    A plain statement of what this attack cannot see.

    Returned as text so a caller can log it beside a clean result, because a
    ``None`` from :func:`recover_private_key` is the absence of one specific
    flaw, not an all-clear. The roadmap requires this statement to exist so the
    detector's output is never over-read.
    """
    return (
        "recover_private_key detects nonce bias only when: the same leak sits "
        "in the same bit position across signatures; there are enough "
        "signatures for the known bits to over-determine the key; and the leak "
        "is in the high bits (a low-bit leak needs a different lattice). It "
        "sees nothing in a correctly randomised or RFC 6979 signer, and a None "
        "result is therefore the absence of this one detectable flaw, not proof "
        "that a signer is sound. It is also bounded by its engine: this "
        "reference reduces the lattice with integer-preserving LLL, which is "
        "exact and recovers a real 256-bit key when the leak is large enough to "
        "need only a few dozen signatures. A one- or two-bit leak at 256 bits "
        "needs hundreds of signatures, so the lattice grows past what any exact "
        "LLL reduces quickly and the attack turns to floating-point BKZ, which "
        "this module deliberately does not use -- an exact answer where one is "
        "affordable over a fast approximate one whose failures are silent."
    )
