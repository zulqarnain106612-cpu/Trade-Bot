"""
Threshold key splitting: no single machine holds a spendable key.

Owns the ``threshold-signatures`` registry entry.

A compromised trading host is the standard threat, and the only structural
defence is to never let one machine hold a key that can move funds. Threshold
splitting does that: the signing key is shared with Shamir's scheme over the
curve's scalar field, so any ``t`` of ``m`` participants can act together and
any ``t-1`` learn nothing. The group public key is the same for every valid
reconstruction, so the outside world sees one key while no one place holds it.

This module builds on :mod:`src.mathcore.fields.interpolation` for the sharing
and :mod:`src.mathcore.curves.secp256k1` for the public key, over the scalar
field of order ``n``. It exposes the Lagrange coefficients at zero as well as
full reconstruction, because a real threshold signer combines *partial*
signatures with those coefficients and never reconstructs the key on one
machine -- reconstructing it, even briefly, would recreate the single point of
failure the scheme exists to remove.

**Not constant time**, and it handles the secret directly on reconstruction, so
it is a reference for the scheme, not a production signer. The warning in
:mod:`src.mathcore.fields.interpolation` about caller-supplied randomness
applies here too.

References: Shamir (1979); Gennaro et al., *Robust Threshold DSS* (1996);
Komlo & Goldberg, *FROST* (2020).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..curves.secp256k1 import CURVE_ORDER, GENERATOR, Point, scalar_multiply
from ..fields.interpolation import shamir_reconstruct, shamir_split
from ..fields.prime_field import PrimeField

__all__ = [
    "SCALAR_FIELD",
    "group_public_key",
    "lagrange_coefficients_at_zero",
    "reconstruct_secret",
    "split_signing_key",
]

# Threshold sharing for secp256k1 lives in the scalar field: the field of
# exponents modulo the group order, not the coordinate field.
SCALAR_FIELD = PrimeField(CURVE_ORDER)


def split_signing_key(
    private_key: int,
    threshold: int,
    coefficients: Sequence[int],
    participant_ids: Sequence[int],
) -> list[tuple[int, int]]:
    """
    Split ``private_key`` into ``t``-of-``m`` shares over the scalar field.

    A thin, curve-specific wrapper over
    :func:`~src.mathcore.fields.interpolation.shamir_split`: it fixes the field
    to the secp256k1 scalar field and checks the key is a valid scalar in
    ``[1, n)``, so a caller cannot share a key the curve cannot use. The
    ``coefficients`` are the caller's secret randomness, as in the underlying
    split. Returns one ``(id, share)`` per participant. Not constant time.
    """
    if not 1 <= private_key < CURVE_ORDER:
        raise ValueError("private_key must be a scalar in [1, n)")
    return shamir_split(private_key, threshold, coefficients, participant_ids, SCALAR_FIELD)


def reconstruct_secret(shares: Sequence[tuple[int, int]]) -> int:
    """
    Reconstruct the signing key from ``threshold`` or more shares.

    Lagrange interpolation at zero over the scalar field. This recreates the
    key on one machine and so is for recovery and testing, not for signing --
    a threshold *signer* combines partial signatures with
    :func:`lagrange_coefficients_at_zero` and never assembles the key. Not
    constant time.
    """
    return shamir_reconstruct(shares, SCALAR_FIELD)


def lagrange_coefficients_at_zero(participant_ids: Sequence[int]) -> list[int]:
    """
    The Lagrange coefficients that weight each share when combining at zero.

    For share ``i`` the coefficient is ``prod_{j != i} x_j / (x_j - x_i)`` over
    the scalar field, so ``sum_i coeff_i * share_i == secret``. Returning these
    is what lets a threshold signer form ``sum_i coeff_i * partial_sig_i``
    without ever reconstructing the key -- the coefficients are public (they
    depend only on which participants signed), the partial signatures stay on
    their own machines. Distinct non-zero ids are required. Not constant time.
    """
    xs = [x % CURVE_ORDER for x in participant_ids]
    if len(set(xs)) != len(xs):
        raise ValueError("participant ids must be distinct")
    if any(x == 0 for x in xs):
        raise ValueError("participant ids must be non-zero")

    field = SCALAR_FIELD
    coefficients = []
    for i, xi in enumerate(xs):
        numerator = 1
        denominator = 1
        for j, xj in enumerate(xs):
            if i == j:
                continue
            numerator = field.mul(numerator, xj)
            denominator = field.mul(denominator, field.sub(xj, xi))
        coefficients.append(field.div(numerator, denominator))
    return coefficients


def group_public_key(private_key: int) -> Point:
    """
    The public key the group presents: ``private_key * G``.

    The single key the outside world verifies against, even though no machine
    holds ``private_key`` once it is split. Provided so a caller can confirm a
    reconstructed key matches the expected group key. Not constant time.
    """
    if not 1 <= private_key < CURVE_ORDER:
        raise ValueError("private_key must be a scalar in [1, n)")
    return scalar_multiply(private_key, GENERATOR)
