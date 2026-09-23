"""
Verifiable threshold secret sharing on secp256k1.

Owns the ``threshold-signatures`` registry entry.

Threshold cryptography splits signing authority so that no single machine holds
a spendable key. That is the only structural defence against a compromised
trading host: a host that cannot sign alone cannot be made to sign alone.

This module supplies the **public, verifiable half** of that construction:

- Feldman commitments ``A_j = a_j * G`` to a dealer's polynomial, which turn
  Shamir's sharing into a *verifiable* one -- a participant can check the share
  they were handed is a real point on the dealer's polynomial rather than
  garbage, without learning the secret and without trusting the dealer.
- The group public key, which is the commitment to the constant term.
- Each participant's public key, needed by FROST to verify partial signatures.
- The Lagrange coefficient at zero for a chosen signer set, which is the weight
  a threshold scheme applies to each contribution.

**It deliberately does not sign, and adding signing here would be the bug.**
The registry entry says why: naive multi-round threshold Schnorr is broken by
the Wagner ROS attack, where an adversary opening many concurrent signing
sessions solves for a forgery across them. The defence is a scheme with a proof
of concurrent security -- FROST (RFC 9591) or MuSig2 -- not an aggregation
assembled from the pieces below. Those pieces are individually correct and
compose into something exploitable, which is the most dangerous shape a crypto
library can have, so the boundary is drawn in the module rather than in a
comment: there is no nonce generation, no nonce aggregation, no challenge
computation and no partial-signature combination here, and
the accompanying test module asserts structurally that none appears later.

Secret sharing itself lives in :mod:`src.mathcore.fields.interpolation`, which
owns ``lagrange-interpolation``. This module does not reimplement it; it
commits to it and verifies against it.

**Not constant time**, and shares are secret. Like
:mod:`src.mathcore.fields.interpolation`, this is a correctness reference for
the scheme rather than a hardened implementation, and it never invents
randomness -- the caller supplies the polynomial coefficients, because their
quality is the entire security of the sharing.

References: RFC 9591 (FROST); BIP-327 (MuSig2); Feldman, "A practical scheme
for non-interactive verifiable secret sharing" (1987); Benhamouda et al. and
Drijvers et al. on ROS / concurrent-session attacks.

Registry: SECR-013, SECR-014 (config/quality_registry.json).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.mathcore.curves.secp256k1 import (
    CURVE_ORDER,
    GENERATOR,
    Point,
    add,
    negate,
    scalar_multiply,
)
from src.mathcore.fields.interpolation import shamir_reconstruct, shamir_split
from src.mathcore.fields.prime_field import PrimeField

# The scalar field of secp256k1. Shares and coefficients are field elements
# here, not integers mod p: the curve's *order* is the modulus that matters for
# anything that will be multiplied into a point.
SCALAR_FIELD = PrimeField(CURVE_ORDER)


class ThresholdError(ValueError):
    """
    A sharing or verification that must not be allowed to return a value.

    A subclass of ValueError so a caller guarding against bad input still
    catches it, and a distinct type so the security-relevant refusals -- a
    share that does not match the dealer's commitments, a signer set smaller
    than the threshold -- can be asserted on specifically.
    """


@dataclass(frozen=True)
class SharingCommitment:
    """
    A dealer's public commitment to one degree-``threshold - 1`` polynomial.

    Carries only public data: ``commitments[j]`` is ``a_j * G``, so the
    polynomial's coefficients -- the secret and the randomness -- are not
    recoverable from it. That is the point of Feldman's scheme, and it is why
    this object is the thing a dealer broadcasts.

    Frozen, because every participant verifies against the same commitment and
    one holder mutating it in place would make some verifications pass and
    others fail with no error anywhere.
    """

    commitments: tuple[Point, ...]

    def __post_init__(self) -> None:
        if not self.commitments:
            raise ThresholdError("a sharing commitment needs at least one point")
        for index, point in enumerate(self.commitments):
            if point.x is None:
                raise ThresholdError(
                    f"commitment {index} is the point at infinity, which commits to "
                    "the zero coefficient and proves nothing"
                )

    @property
    def threshold(self) -> int:
        """The number of shares required to reconstruct: the polynomial degree plus one."""
        return len(self.commitments)

    @property
    def group_public_key(self) -> Point:
        """
        The public key of the shared secret: the commitment to the constant term.

        ``A_0 = a_0 * G`` and ``a_0`` is the secret, so this is exactly the
        public key that a full threshold signature will verify against.
        """
        return self.commitments[0]


def _require_threshold(threshold: int) -> None:
    if threshold < 1:
        raise ThresholdError(f"threshold must be at least 1, got {threshold}")


def _require_nonzero_distinct(share_xs: Sequence[int]) -> None:
    if not share_xs:
        raise ThresholdError("no participant identifiers given")
    reduced = [x % CURVE_ORDER for x in share_xs]
    if any(x == 0 for x in reduced):
        raise ThresholdError(
            "participant identifier 0 is not allowed: x = 0 evaluates the "
            "polynomial at its constant term, which is the secret itself"
        )
    if len(set(reduced)) != len(reduced):
        raise ThresholdError(
            "participant identifiers must be distinct modulo the curve order; "
            "two holders of the same x hold the same share and add no threshold"
        )


def commit_to_polynomial(secret: int, coefficients: Sequence[int]) -> SharingCommitment:
    """
    Commit to the polynomial ``secret + c_1 x + ... + c_{t-1} x^{t-1}``.

    Takes the same ``secret`` and ``coefficients`` that
    :func:`~src.mathcore.fields.interpolation.shamir_split` takes, so the
    commitment and the shares are guaranteed to describe one polynomial rather
    than two that happen to look consistent.

    The caller supplies the coefficients; this module has no random source, by
    the same reasoning as the interpolation module it builds on. Coefficients
    from a weak generator make the sharing reconstructible by an attacker, and
    that is a property of the caller's entropy, so the caller must own it
    visibly.
    """
    reduced_secret = secret % CURVE_ORDER
    if reduced_secret == 0:
        raise ThresholdError(
            "the secret reduces to 0 mod the curve order: its public key would "
            "be the point at infinity, which verifies every signature"
        )
    points = [scalar_multiply(reduced_secret, GENERATOR)]
    for index, coefficient in enumerate(coefficients, start=1):
        if coefficient % CURVE_ORDER == 0:
            raise ThresholdError(
                f"coefficient {index} reduces to 0 mod the curve order, lowering "
                "the polynomial's degree and therefore the real threshold below "
                "the declared one"
            )
        points.append(scalar_multiply(coefficient % CURVE_ORDER, GENERATOR))
    return SharingCommitment(commitments=tuple(points))


def split_secret(
    secret: int,
    threshold: int,
    coefficients: Sequence[int],
    share_xs: Sequence[int],
) -> tuple[SharingCommitment, list[tuple[int, int]]]:
    """
    Produce a verifiable sharing: the public commitment and the secret shares.

    Returns both together deliberately. A dealer that broadcasts a commitment
    from one polynomial and distributes shares from another has produced a
    sharing where every individual check passes and reconstruction yields the
    wrong secret; deriving both from the same inputs in one call is what makes
    that impossible rather than merely discouraged.

    The returned shares are **secret**. Only the commitment is safe to publish.
    """
    _require_threshold(threshold)
    _require_nonzero_distinct(share_xs)
    if len(coefficients) != threshold - 1:
        raise ThresholdError(
            f"need exactly {threshold - 1} coefficients for threshold {threshold}, "
            f"got {len(coefficients)}"
        )
    if len(share_xs) < threshold:
        raise ThresholdError(
            f"{len(share_xs)} participants cannot satisfy a threshold of "
            f"{threshold}: the secret would be unrecoverable by anyone"
        )

    commitment = commit_to_polynomial(secret, coefficients)
    shares = shamir_split(
        secret % CURVE_ORDER,
        threshold,
        [c % CURVE_ORDER for c in coefficients],
        [x % CURVE_ORDER for x in share_xs],
        SCALAR_FIELD,
    )
    return commitment, shares


def participant_public_key(x: int, commitment: SharingCommitment) -> Point:
    """
    The public key of the share at ``x``: ``sum_j A_j * x^j``.

    This is the polynomial evaluated "in the exponent". FROST needs it to
    verify a participant's partial signature, and :func:`verify_share` needs it
    to check a share without seeing the secret.
    """
    if x % CURVE_ORDER == 0:
        raise ThresholdError("x = 0 is the group key itself, not a participant")

    power = 1
    total: Point = Point(None, None)
    for coefficient_commitment in commitment.commitments:
        total = add(total, scalar_multiply(power, coefficient_commitment))
        power = SCALAR_FIELD.mul(power, x % CURVE_ORDER)
    return total


def verify_share(x: int, share: int, commitment: SharingCommitment) -> bool:
    """
    Check that ``share`` is the dealer's polynomial evaluated at ``x``.

    ``share * G == sum_j A_j * x^j``. True only if the dealer handed out a
    point on the polynomial they committed to.

    This is the check that makes the sharing *verifiable*, and without it a
    dealer -- or anyone who tampers with a share in transit -- can hand one
    participant a wrong value. The damage surfaces only at reconstruction,
    which for a custody key means the moment funds must move and cannot. The
    share's holder can run this the instant they receive it.

    Returns a bool rather than raising: "is this share valid" is a question a
    caller asks about untrusted input, and the answer False is not an error.
    Every function above raises instead, because there a malformed argument is
    a programming mistake.
    """
    if x % CURVE_ORDER == 0:
        return False
    return scalar_multiply(share % CURVE_ORDER, GENERATOR) == participant_public_key(x, commitment)


def lagrange_coefficient_at_zero(x_i: int, signer_xs: Sequence[int]) -> int:
    """
    The weight ``lambda_i`` that participant ``x_i`` carries in a signer set.

    ``lambda_i = prod_{j != i} x_j / (x_j - x_i)``, the Lagrange basis
    polynomial evaluated at zero. A threshold scheme scales each contribution
    by it so the weighted sum reconstructs the value at ``x = 0``.

    **The coefficient depends on the whole signer set, not just on ``x_i``.**
    That is the subtlety worth stating: a participant who computes their weight
    against a different set than the one that actually signs produces a
    contribution that does not combine, and a scheme that lets an adversary
    choose the set after seeing contributions is a distinct attack. Whoever
    calls this must fix the signer set first.
    """
    _require_nonzero_distinct(signer_xs)
    reduced_i = x_i % CURVE_ORDER
    reduced_set = [x % CURVE_ORDER for x in signer_xs]
    if reduced_i not in reduced_set:
        raise ThresholdError(
            f"x_i={x_i} is not in the signer set; a weight computed against a "
            "set the participant is not part of cannot combine"
        )

    numerator = 1
    denominator = 1
    for x_j in reduced_set:
        if x_j == reduced_i:
            continue
        numerator = SCALAR_FIELD.mul(numerator, x_j)
        denominator = SCALAR_FIELD.mul(denominator, SCALAR_FIELD.sub(x_j, reduced_i))
    return SCALAR_FIELD.div(numerator, denominator)


def recover_secret(shares: Sequence[tuple[int, int]], threshold: int) -> int:
    """
    Reconstruct the shared secret from at least ``threshold`` shares.

    Refuses fewer than ``threshold`` shares rather than returning the wrong
    value. Interpolating ``k < t`` points yields a different polynomial's
    constant term -- a number, indistinguishable from the real secret, which
    for a custody key means signing with a key nobody controls. Shamir's
    guarantee is that ``t`` shares are needed *and sufficient*; a caller who
    passes fewer has made an error the scheme cannot detect on its own, so this
    layer detects it by counting.

    Duplicate identifiers are refused for the same reason: three shares from
    two distinct holders is two points, and would silently under-determine the
    polynomial while appearing to meet a threshold of three.
    """
    _require_threshold(threshold)
    if len(shares) < threshold:
        raise ThresholdError(
            f"{len(shares)} shares cannot reconstruct a {threshold}-of-n secret; "
            "interpolating too few points yields a wrong value, not an error"
        )
    _require_nonzero_distinct([x for x, _ in shares])
    return shamir_reconstruct(
        [(x % CURVE_ORDER, y % CURVE_ORDER) for x, y in shares[:threshold]],
        SCALAR_FIELD,
    )


def verify_sharing(
    commitment: SharingCommitment,
    shares: Sequence[tuple[int, int]],
) -> list[int]:
    """
    Verify every share against the commitment, returning the invalid identifiers.

    An empty list means the sharing is consistent. Returning the offenders
    rather than a bool is what makes this usable in a distributed key
    generation round: the answer a coordinator needs is *which* participant was
    given a bad share, because that names either the dealer's mistake or the
    tampered link.
    """
    return [x for x, share in shares if not verify_share(x, share, commitment)]


def group_public_key_from_shares(shares: Sequence[tuple[int, int]], threshold: int) -> Point:
    """
    Derive the group public key from shares, without reconstructing the secret.

    ``sum_i lambda_i * share_i * G`` over a threshold-sized signer set. Useful
    to confirm a set of shares belongs to the key a system expects *before*
    relying on them, and it is the identity that every threshold signature
    scheme's verification rests on.

    This still touches share values, so it is not a public-side operation --
    run it where the shares already are, not on a coordinator. It reconstructs
    the public key only; the secret scalar is never formed.
    """
    _require_threshold(threshold)
    if len(shares) < threshold:
        raise ThresholdError(
            f"{len(shares)} shares cannot determine the group key for a threshold of {threshold}"
        )
    chosen = list(shares[:threshold])
    _require_nonzero_distinct([x for x, _ in chosen])
    signer_xs = [x % CURVE_ORDER for x, _ in chosen]

    total: Point = Point(None, None)
    for x, share in chosen:
        weight = lagrange_coefficient_at_zero(x, signer_xs)
        scaled = SCALAR_FIELD.mul(weight, share % CURVE_ORDER)
        # No special case for scaled == 0: 0*G is the identity and add()
        # handles it, so a guard here would be a branch that changes nothing
        # and is unreachable in any test -- which is worse than no guard.
        total = add(total, scalar_multiply(scaled, GENERATOR))
    return total


def shares_agree_with_group_key(
    shares: Sequence[tuple[int, int]],
    threshold: int,
    expected: Point,
) -> bool:
    """
    Whether ``shares`` reconstruct to ``expected``, checked in the group.

    The operational question before a custody key is trusted: do the shares
    this system holds actually belong to the key it thinks they do? Answered
    without ever forming the secret scalar, and without a timing-sensitive
    comparison of one, because the comparison is between two public points.
    """
    if expected.x is None:
        raise ThresholdError(
            "the point at infinity is not a valid group key; it verifies every "
            "signature, so comparing against it would always succeed"
        )
    return group_public_key_from_shares(shares, threshold) == expected


def refute_share(x: int, share: int, commitment: SharingCommitment) -> Point | None:
    """
    The discrepancy point for an invalid share, or None if the share is valid.

    ``share * G - sum_j A_j * x^j``. Non-None means the share disagrees with
    the commitment, and the value is the difference, which a dispute round can
    publish as evidence without revealing a correct share.

    This exists so that "your share is wrong" is a demonstrable claim rather
    than an assertion. In a distributed key generation where participants do
    not trust each other, a complaint nobody can check is indistinguishable
    from a false accusation, and a protocol that cannot tell those apart can be
    stalled by anyone.
    """
    if x % CURVE_ORDER == 0:
        raise ThresholdError("x = 0 is the group key itself, not a participant")
    expected = participant_public_key(x, commitment)
    actual = scalar_multiply(share % CURVE_ORDER, GENERATOR)
    if actual == expected:
        return None
    difference = add(actual, negate(expected))
    if difference.x is None:  # pragma: no cover - equal points are caught above
        return None
    return difference
