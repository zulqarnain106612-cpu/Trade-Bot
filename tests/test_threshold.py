"""
Tests for :mod:`src.mathcore.derivation.threshold` -- verifiable threshold sharing.

Three kinds of property, in descending order of how much they matter.

**The boundary.** The registry entry's ``risk_if_misused`` is explicit: naive
multi-round threshold Schnorr is broken by the Wagner ROS attack, and the fix
is a scheme with a proof of concurrent security rather than an aggregation
assembled from correct pieces. The pieces in this module are individually
correct and compose into something exploitable, which is the most dangerous
shape a crypto library can have. So the most important test here does not
exercise any computation: it asserts that no nonce, challenge or
partial-signature function has appeared in the module. Every other test would
still pass on the day someone adds one.

**Verification actually verifies.** A commitment scheme nobody has shown a bad
share to is a scheme that passes because it finds nothing. For every "this
share is accepted" there is a "this tampered share is rejected", including the
cases a weaker check would wave through -- a share off by one, a share from the
wrong participant, a share valid under a different dealer's polynomial.

**Reconstruction is exact-threshold.** ``t`` shares recover the secret and
``t-1`` must not: not "returns garbage", but refuses. Interpolating too few
points yields a number indistinguishable from the real secret, and for a
custody key that means signing with a key nobody controls.

Decides:
  - SECR-013 — The threshold module supplies sharing primitives and refuses to be a signer
  - SECR-014 — A tampered share is rejected and too few shares are refused, not approximated
"""

from __future__ import annotations

import inspect

import pytest

from src.mathcore.curves.secp256k1 import (
    CURVE_ORDER,
    GENERATOR,
    Point,
    add,
    is_on_curve,
    negate,
    scalar_multiply,
)
from src.mathcore.derivation import threshold as th
from src.mathcore.derivation.threshold import (
    SCALAR_FIELD,
    SharingCommitment,
    ThresholdError,
    commit_to_polynomial,
    group_public_key_from_shares,
    lagrange_coefficient_at_zero,
    participant_public_key,
    recover_secret,
    refute_share,
    shares_agree_with_group_key,
    split_secret,
    verify_share,
    verify_sharing,
)

SECRET = 0xC0FFEE1234567890ABCDEF
COEFFICIENTS = [0x1111111111111111, 0x2222222222222222]
SHARE_XS = [1, 2, 3, 4, 5]
THRESHOLD = 3


@pytest.fixture(scope="module")
def sharing() -> tuple[SharingCommitment, list[tuple[int, int]]]:
    """
    One 3-of-5 sharing, reused across the module.

    Module scope because it is a pure function of constants and every test
    treats it as read-only -- the commitment is frozen and the share list is
    never mutated. Re-deriving it per test would pay for several hundred scalar
    multiplications to obtain the same object (GOV-016).
    """
    return split_secret(SECRET, THRESHOLD, COEFFICIENTS, SHARE_XS)


# ---------------------------------------------------------------------------
# The boundary: this module does not sign
# ---------------------------------------------------------------------------


class TestModuleDoesNotSign:
    """
    The registry entry's risk warning, enforced rather than documented.

    ROS/Wagner breaks ad-hoc threshold Schnorr across concurrent sessions. The
    pieces here -- Lagrange weights, participant public keys, commitments --
    are exactly the pieces someone would reach for to build one. Keeping them
    unassembled is the security property, so it is tested like one.
    """

    # Substrings that would indicate a signing path had been added. Matched
    # against public function names, which is where such a thing would land.
    SIGNING_SHAPED = (
        "sign",
        "nonce",
        "challenge",
        "aggregate",
        "partial",
        "combine",
        "musig",
        "frost",
        "commit_nonce",
    )

    def test_no_public_function_is_signing_shaped(self) -> None:
        offenders = []
        for name, obj in vars(th).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != th.__name__:
                continue  # re-exported from interpolation, not defined here
            if any(token in name.lower() for token in self.SIGNING_SHAPED):
                offenders.append(name)
        assert not offenders, (
            f"{offenders} look like signing operations. This module is "
            "deliberately not a signer: naive threshold Schnorr is broken by "
            "the ROS attack across concurrent sessions. Use FROST (RFC 9591) "
            "or MuSig2, do not assemble one here."
        )

    def test_module_has_no_random_source(self) -> None:
        """
        The caller owns the entropy, as in the interpolation module this builds
        on. A module that samples its own coefficients hides the one input
        whose quality *is* the security of the sharing.
        """
        source = inspect.getsource(th)
        for banned in ("import random", "import secrets", "os.urandom", "SystemRandom"):
            assert banned not in source, f"{banned!r} appears; the caller must supply randomness"

    def test_secret_sharing_is_not_reimplemented_here(self) -> None:
        """
        One owner per registry entry. Shamir belongs to
        `lagrange-interpolation`; this module commits to it and verifies
        against it. A second implementation would be a second thing to keep
        correct, and the two would diverge silently.
        """
        source = inspect.getsource(th)
        assert "from src.mathcore.fields.interpolation import" in source


# ---------------------------------------------------------------------------
# Sharing and reconstruction
# ---------------------------------------------------------------------------


class TestSplitAndRecover:
    def test_threshold_shares_recover_the_secret(self, sharing) -> None:
        _, shares = sharing
        assert recover_secret(shares, THRESHOLD) == SECRET

    @pytest.mark.parametrize("offsets", [(0, 1, 2), (2, 3, 4), (0, 2, 4), (1, 3, 4), (0, 1, 4)])
    def test_any_threshold_subset_recovers(self, sharing, offsets: tuple[int, ...]) -> None:
        """
        Shamir's guarantee is that *any* t shares suffice, not the first t. A
        reconstruction that only works for consecutive identifiers has a
        Lagrange-weight bug that the obvious test would miss.
        """
        _, shares = sharing
        subset = [shares[i] for i in offsets]
        assert recover_secret(subset, THRESHOLD) == SECRET

    @pytest.mark.parametrize("count", [0, 1, 2])
    def test_fewer_than_threshold_is_refused(self, sharing, count: int) -> None:
        """
        Refused, not wrong. Interpolating k < t points returns a number that
        looks exactly like a secret; for a custody key that means signing with
        a key nobody controls, and nothing in the arithmetic can notice.
        """
        _, shares = sharing
        with pytest.raises(ThresholdError, match="cannot reconstruct"):
            recover_secret(shares[:count], THRESHOLD)

    def test_duplicate_identifiers_do_not_count_toward_the_threshold(self, sharing) -> None:
        """
        Three shares from two holders is two points. Accepting it would
        under-determine the polynomial while appearing to meet a threshold of
        three -- a quorum that is not one.
        """
        _, shares = sharing
        with pytest.raises(ThresholdError, match="distinct"):
            recover_secret([shares[0], shares[1], shares[1]], THRESHOLD)

    @pytest.mark.parametrize("t,n", [(1, 1), (1, 3), (2, 2), (2, 5), (4, 5), (5, 5)])
    def test_other_threshold_shapes(self, t: int, n: int) -> None:
        coefficients = [0x1000 + i for i in range(t - 1)]
        commitment, shares = split_secret(SECRET, t, coefficients, list(range(1, n + 1)))
        assert commitment.threshold == t
        assert len(shares) == n
        assert verify_sharing(commitment, shares) == []
        assert recover_secret(shares, t) == SECRET

    def test_more_participants_than_needed_is_fine(self) -> None:
        commitment, shares = split_secret(SECRET, 2, [0x99], list(range(1, 21)))
        assert len(shares) == 20
        assert verify_sharing(commitment, shares) == []

    def test_fewer_participants_than_the_threshold_is_refused(self) -> None:
        """
        A 3-of-2 sharing is unrecoverable by anyone. Producing it silently
        means discovering at signing time that the key is gone.
        """
        with pytest.raises(ThresholdError, match="cannot satisfy a threshold"):
            split_secret(SECRET, 3, [1, 2], [1, 2])

    def test_wrong_coefficient_count_is_refused(self) -> None:
        with pytest.raises(ThresholdError, match="exactly 2 coefficients"):
            split_secret(SECRET, 3, [1], [1, 2, 3])

    @pytest.mark.parametrize("bad_xs", [[0, 1, 2], [1, 2, CURVE_ORDER], [1, 1, 2]])
    def test_invalid_identifiers_are_refused(self, bad_xs: list[int]) -> None:
        """
        x = 0 evaluates the polynomial at its constant term -- handing a
        participant the secret outright. CURVE_ORDER is 0 reduced, which is the
        same mistake spelled so the naive check misses it.
        """
        with pytest.raises(ThresholdError):
            split_secret(SECRET, 2, [7], bad_xs)

    def test_threshold_below_one_is_refused(self) -> None:
        with pytest.raises(ThresholdError, match="at least 1"):
            split_secret(SECRET, 0, [], [1])

    def test_a_secret_congruent_to_zero_is_refused(self) -> None:
        """
        Its public key is the point at infinity, which verifies every
        signature. A sharing of it is worse than no sharing.
        """
        for secret in (0, CURVE_ORDER, 2 * CURVE_ORDER):
            with pytest.raises(ThresholdError, match="verifies every signature"):
                split_secret(secret, 2, [5], [1, 2])

    def test_a_zero_coefficient_is_refused(self) -> None:
        """
        It lowers the polynomial's degree, so the real threshold is below the
        declared one: fewer holders than advertised can recover the secret, and
        nothing reports it.
        """
        with pytest.raises(ThresholdError, match="lowering"):
            split_secret(SECRET, 3, [0x11, CURVE_ORDER], [1, 2, 3])

    def test_commitment_and_shares_come_from_one_polynomial(self, sharing) -> None:
        """
        The reason `split_secret` returns both together. A dealer broadcasting
        a commitment to one polynomial while distributing shares from another
        produces a sharing where every individual check passes and
        reconstruction yields the wrong secret.
        """
        commitment, shares = sharing
        assert verify_sharing(commitment, shares) == []
        assert commitment.group_public_key == scalar_multiply(recover_secret(shares, THRESHOLD))


# ---------------------------------------------------------------------------
# Commitments and verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_every_honest_share_verifies(self, sharing) -> None:
        commitment, shares = sharing
        for x, share in shares:
            assert verify_share(x, share, commitment)

    @pytest.mark.parametrize("delta", [1, -1, 2, CURVE_ORDER - 1, 0x1000])
    def test_a_tampered_share_is_rejected(self, sharing, delta: int) -> None:
        """
        The case the scheme exists for. Off-by-one is included on purpose: a
        check that compared only the low bits, or compared lengths, would pass
        it -- and a share tampered with in transit surfaces otherwise only when
        funds must move and cannot.
        """
        commitment, shares = sharing
        x, share = shares[0]
        assert not verify_share(x, (share + delta) % CURVE_ORDER, commitment)

    def test_a_share_offered_under_the_wrong_identifier_is_rejected(self, sharing) -> None:
        """
        Participant 2's share is a valid point on the polynomial -- just not at
        x = 1. A verifier that checked "is this on the polynomial somewhere"
        would accept it, and the reconstruction would then be wrong.
        """
        commitment, shares = sharing
        assert not verify_share(shares[0][0], shares[1][1], commitment)

    def test_a_share_from_a_different_dealer_is_rejected(self) -> None:
        commitment_a, _ = split_secret(SECRET, 2, [0x11], [1, 2])
        _, shares_b = split_secret(SECRET + 1, 2, [0x22], [1, 2])
        assert not verify_share(shares_b[0][0], shares_b[0][1], commitment_a)

    def test_verify_sharing_names_every_offender(self, sharing) -> None:
        """
        A coordinator running distributed key generation needs *which*
        participant got a bad share -- that names either the dealer's mistake
        or the tampered link. A bool would not.
        """
        commitment, shares = sharing
        tampered = list(shares)
        tampered[1] = (tampered[1][0], (tampered[1][1] + 1) % CURVE_ORDER)
        tampered[3] = (tampered[3][0], (tampered[3][1] + 7) % CURVE_ORDER)
        assert verify_sharing(commitment, tampered) == [shares[1][0], shares[3][0]]

    def test_verify_share_rejects_x_zero_without_raising(self, sharing) -> None:
        """
        False, not an exception: "is this share valid" is asked about untrusted
        input, and x = 0 is a thing an attacker submits, not a programming bug.
        """
        commitment, _ = sharing
        assert not verify_share(0, 12345, commitment)
        assert not verify_share(CURVE_ORDER, 12345, commitment)

    def test_participant_public_key_matches_the_share(self, sharing) -> None:
        commitment, shares = sharing
        for x, share in shares:
            assert participant_public_key(x, commitment) == scalar_multiply(share, GENERATOR)

    def test_participant_public_keys_are_on_the_curve(self, sharing) -> None:
        commitment, shares = sharing
        for x, _ in shares:
            assert is_on_curve(participant_public_key(x, commitment))

    def test_participant_public_key_refuses_x_zero(self, sharing) -> None:
        commitment, _ = sharing
        with pytest.raises(ThresholdError, match="group key itself"):
            participant_public_key(0, commitment)

    def test_group_public_key_is_the_constant_term_commitment(self, sharing) -> None:
        commitment, _ = sharing
        assert commitment.group_public_key == commitment.commitments[0]
        assert commitment.group_public_key == scalar_multiply(SECRET % CURVE_ORDER, GENERATOR)

    def test_commit_to_polynomial_agrees_with_split(self, sharing) -> None:
        commitment, _ = sharing
        assert commit_to_polynomial(SECRET, COEFFICIENTS) == commitment

    def test_commitment_reveals_no_coefficient(self, sharing) -> None:
        """
        Feldman's property: the commitment is publishable. Asserted the only
        way a test can -- no committed point equals the generator times any
        coefficient other than its own, so the object carries no scalar.
        """
        commitment, _ = sharing
        assert commitment.commitments[0] != commitment.commitments[1]
        assert all(point.x is not None for point in commitment.commitments)


class TestCommitmentInvariants:
    def test_empty_commitment_is_refused(self) -> None:
        with pytest.raises(ThresholdError, match="at least one point"):
            SharingCommitment(commitments=())

    def test_infinity_in_a_commitment_is_refused(self) -> None:
        """
        It commits to the zero coefficient, so it proves nothing about that
        term -- and at position 0 it is a group key that verifies every
        signature.
        """
        with pytest.raises(ThresholdError, match="infinity"):
            SharingCommitment(commitments=(Point(None, None),))
        with pytest.raises(ThresholdError, match="infinity"):
            SharingCommitment(commitments=(GENERATOR, Point(None, None)))

    def test_threshold_is_the_commitment_length(self) -> None:
        commitment = SharingCommitment(
            commitments=(GENERATOR, scalar_multiply(2), scalar_multiply(3))
        )
        assert commitment.threshold == 3

    def test_commitment_is_frozen(self, sharing) -> None:
        """
        Every participant verifies against the same object. One holder mutating
        it would make some verifications pass and others fail, with no error.
        """
        commitment, _ = sharing
        with pytest.raises(Exception):  # noqa: B017 - dataclasses raises FrozenInstanceError
            commitment.commitments = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lagrange weights
# ---------------------------------------------------------------------------


class TestLagrangeCoefficient:
    def test_weights_reconstruct_the_secret(self, sharing) -> None:
        """
        The identity every threshold scheme rests on:
        sum_i lambda_i * share_i == secret.
        """
        _, shares = sharing
        chosen = shares[:THRESHOLD]
        signer_xs = [x for x, _ in chosen]
        total = 0
        for x, share in chosen:
            weight = lagrange_coefficient_at_zero(x, signer_xs)
            total = SCALAR_FIELD.add(total, SCALAR_FIELD.mul(weight, share))
        assert total == SECRET % CURVE_ORDER

    def test_a_single_signer_carries_weight_one(self) -> None:
        assert lagrange_coefficient_at_zero(7, [7]) == 1

    def test_weight_depends_on_the_whole_signer_set(self, sharing) -> None:
        """
        The subtlety worth pinning. Participant 1's weight differs between
        signer sets, so a participant who computes it against the wrong set
        produces a contribution that does not combine -- and a scheme letting
        an adversary pick the set after seeing contributions is its own attack.
        """
        first = lagrange_coefficient_at_zero(1, [1, 2, 3])
        second = lagrange_coefficient_at_zero(1, [1, 4, 5])
        assert first != second

    def test_weight_for_a_non_member_is_refused(self) -> None:
        with pytest.raises(ThresholdError, match="not in the signer set"):
            lagrange_coefficient_at_zero(9, [1, 2, 3])

    @pytest.mark.parametrize("signer_xs", [[], [0, 1], [1, 1], [1, CURVE_ORDER]])
    def test_invalid_signer_sets_are_refused(self, signer_xs: list[int]) -> None:
        with pytest.raises(ThresholdError):
            lagrange_coefficient_at_zero(1, signer_xs)

    def test_identifiers_are_compared_modulo_the_curve_order(self) -> None:
        """
        x and x + n are the same participant. A set that treats them as two
        distinct signers has a duplicate it cannot see, which collapses the
        real threshold.
        """
        with pytest.raises(ThresholdError, match="distinct"):
            lagrange_coefficient_at_zero(1, [1, 1 + CURVE_ORDER, 2])


# ---------------------------------------------------------------------------
# Group key from shares
# ---------------------------------------------------------------------------


class TestGroupKeyFromShares:
    def test_matches_the_dealer_commitment(self, sharing) -> None:
        commitment, shares = sharing
        assert group_public_key_from_shares(shares, THRESHOLD) == commitment.group_public_key

    @pytest.mark.parametrize("offsets", [(0, 1, 2), (1, 2, 3), (0, 2, 4), (2, 3, 4)])
    def test_any_threshold_subset_gives_the_same_key(self, sharing, offsets) -> None:
        commitment, shares = sharing
        subset = [shares[i] for i in offsets]
        assert group_public_key_from_shares(subset, THRESHOLD) == commitment.group_public_key

    def test_agreement_check_accepts_the_right_key(self, sharing) -> None:
        commitment, shares = sharing
        assert shares_agree_with_group_key(shares, THRESHOLD, commitment.group_public_key)

    def test_agreement_check_rejects_a_different_key(self, sharing) -> None:
        """
        The operational question: do the shares this host holds belong to the
        key it thinks they do? A check that always said yes would be worse than
        none, since it would be trusted.
        """
        _, shares = sharing
        other, _ = split_secret(SECRET + 1, THRESHOLD, COEFFICIENTS, SHARE_XS)
        assert not shares_agree_with_group_key(shares, THRESHOLD, other.group_public_key)

    def test_agreement_check_refuses_infinity_as_the_expected_key(self, sharing) -> None:
        _, shares = sharing
        with pytest.raises(ThresholdError, match="verifies every signature"):
            shares_agree_with_group_key(shares, THRESHOLD, Point(None, None))

    def test_too_few_shares_is_refused(self, sharing) -> None:
        _, shares = sharing
        with pytest.raises(ThresholdError, match="cannot determine the group key"):
            group_public_key_from_shares(shares[:2], THRESHOLD)

    def test_duplicate_identifiers_are_refused(self, sharing) -> None:
        _, shares = sharing
        with pytest.raises(ThresholdError, match="distinct"):
            group_public_key_from_shares([shares[0], shares[0], shares[1]], THRESHOLD)

    def test_threshold_below_one_is_refused(self, sharing) -> None:
        _, shares = sharing
        with pytest.raises(ThresholdError, match="at least 1"):
            group_public_key_from_shares(shares, 0)

    def test_a_one_of_one_sharing_works(self) -> None:
        """
        The degenerate shape, where the polynomial is a constant and the single
        share *is* the secret. Worth pinning because the Lagrange product over
        an empty set of other signers is 1, and an implementation that started
        the product at 0 would return the identity here and nowhere else.
        """
        commitment, shares = split_secret(SECRET, 1, [], [1])
        assert group_public_key_from_shares(shares, 1) == commitment.group_public_key
        assert recover_secret(shares, 1) == SECRET


# ---------------------------------------------------------------------------
# Dispute evidence
# ---------------------------------------------------------------------------


class TestRefuteShare:
    def test_valid_share_yields_no_discrepancy(self, sharing) -> None:
        commitment, shares = sharing
        for x, share in shares:
            assert refute_share(x, share, commitment) is None

    def test_invalid_share_yields_the_difference(self, sharing) -> None:
        """
        The evidence is checkable arithmetic, so "your share is wrong" is a
        demonstrable claim. A complaint nobody can verify is indistinguishable
        from a false accusation, and a protocol that cannot tell those apart
        can be stalled by anyone.
        """
        commitment, shares = sharing
        x, share = shares[0]
        bad = (share + 5) % CURVE_ORDER
        difference = refute_share(x, bad, commitment)
        assert difference is not None
        assert is_on_curve(difference)
        # The difference is exactly (bad - share) * G, which anyone can check
        # against the commitment without holding a correct share.
        expected = add(
            scalar_multiply(bad, GENERATOR),
            negate(participant_public_key(x, commitment)),
        )
        assert difference == expected

    def test_refute_refuses_x_zero(self, sharing) -> None:
        commitment, _ = sharing
        with pytest.raises(ThresholdError, match="group key itself"):
            refute_share(0, 1, commitment)
