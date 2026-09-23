"""
Tests for :mod:`src.mathcore.numbertheory.safe_primes`.

The registry entry's risk is specific: "Unvalidated DH parameters allow a
small-subgroup confinement attack in which the shared secret takes one of a
handful of values." So the tests are built around the two halves of that
defence, and around the fact that having one half is the common real failure --
Valenta et al. found deployments with correct RFC 7919 moduli and no public
value check at all.

**The parameters are a safe-prime group.** A prime modulus is not enough, and
`is_safe_prime` returning True for a prime whose `(p-1)/2` is composite would be
the whole bug. Every prime-but-not-safe modulus in the corpus below is a case
that a naive "is p prime" check passes.

**Every received value is in the prime-order subgroup.** The elements of order
1 and 2 are enumerable in a safe-prime group -- exactly `1` and `p-1` -- so the
check is complete rather than heuristic, and the tests assert that completeness
by enumerating the subgroup directly for small primes and comparing.

No named group's modulus is hardcoded here or in the module. The safe primes
used are small enough to verify by hand or by brute force in the test itself,
which is the only kind of constant worth writing down: `test_the_corpus_is_
what_it_claims` re-derives every one of them from its definition.
"""

from __future__ import annotations

import pytest

from src.mathcore.numbertheory.safe_primes import (
    GroupVerdict,
    SafePrimeError,
    is_safe_prime,
    is_sophie_germain_prime,
    is_valid_public_value,
    order_of,
    smallest_factor_of_p_minus_one,
    subgroup_order,
    validate_group,
)

# p = 2q + 1 with both prime. Small enough that the subgroup can be enumerated
# by brute force, which is how the completeness claims below are checked.
SAFE_PRIMES = [5, 7, 11, 23, 47, 59, 83, 107, 167, 179, 227, 263, 347, 359, 383]

# Primes whose (p-1)/2 is composite. Each one passes a naive "is p prime"
# parameter check and is exactly what the second half of is_safe_prime catches.
PRIME_BUT_NOT_SAFE = [13, 17, 19, 29, 31, 37, 41, 43, 53, 61, 67, 71, 73, 79, 89, 97]

COMPOSITE = [9, 15, 21, 25, 27, 33, 35, 49, 51, 55, 91, 121]


def _naive_prime(n: int) -> bool:
    """Trial division, so the corpus is checked against something independent."""
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    factor = 3
    while factor * factor <= n:
        if n % factor == 0:
            return False
        factor += 2
    return True


class TestTheCorpusIsWhatItClaims:
    """
    The constants in this file, re-derived from their definitions.

    A mislabelled modulus would silently become the definition of correct for
    every test below -- the module would be "verified" against a typo. This
    project has shipped a fabricated constant three times, so a list of primes
    typed into a test file is checked, not trusted.
    """

    @pytest.mark.parametrize("p", SAFE_PRIMES)
    def test_safe_primes_are_safe(self, p: int) -> None:
        assert _naive_prime(p)
        assert _naive_prime((p - 1) // 2)

    @pytest.mark.parametrize("p", PRIME_BUT_NOT_SAFE)
    def test_prime_but_not_safe_really_is_both(self, p: int) -> None:
        assert _naive_prime(p), "must be prime, or it tests nothing about safety"
        assert not _naive_prime((p - 1) // 2), "must not be safe, or it is in the wrong list"

    @pytest.mark.parametrize("n", COMPOSITE)
    def test_composites_are_composite(self, n: int) -> None:
        assert not _naive_prime(n)


class TestIsSafePrime:
    @pytest.mark.parametrize("p", SAFE_PRIMES)
    def test_accepts_a_safe_prime(self, p: int) -> None:
        assert is_safe_prime(p)

    @pytest.mark.parametrize("p", PRIME_BUT_NOT_SAFE)
    def test_rejects_a_prime_whose_half_is_composite(self, p: int) -> None:
        """
        The half that gets dropped, and the whole bug if it is.

        A prime modulus says nothing about subgroup structure: p-1 can still be
        smooth, which is exactly the condition a confinement attack needs.
        """
        assert not is_safe_prime(p)

    @pytest.mark.parametrize("n", COMPOSITE)
    def test_rejects_a_composite(self, n: int) -> None:
        assert not is_safe_prime(n)

    @pytest.mark.parametrize("n", [-7, -1, 0, 1, 2, 3, 4, 6, 8, 10])
    def test_rejects_small_and_non_positive(self, n: int) -> None:
        """A modulus below 5 has no prime-order subgroup to speak of."""
        assert not is_safe_prime(n)

    def test_agrees_with_brute_force_over_a_range(self) -> None:
        """
        Exhaustive over a small range, which no hand-picked corpus can be.

        Catches an off-by-one at the boundary and any modulus where the two
        primality calls disagree with trial division.
        """
        expected = {n for n in range(-5, 500) if _naive_prime(n) and _naive_prime((n - 1) // 2)}
        # (n-1)//2 for n < 5 is degenerate; the module's floor is 5 and the
        # naive expression agrees from there.
        expected = {n for n in expected if n >= 5}
        actual = {n for n in range(-5, 500) if is_safe_prime(n)}
        assert actual == expected


class TestSophieGermain:
    @pytest.mark.parametrize("p", SAFE_PRIMES)
    def test_the_other_side_of_the_pair(self, p: int) -> None:
        """If p = 2q+1 is safe then q is Sophie Germain, by definition."""
        assert is_sophie_germain_prime((p - 1) // 2)

    @pytest.mark.parametrize("q", [7, 13, 17, 19, 31, 37, 43, 47, 67, 73])
    def test_rejects_a_prime_whose_double_plus_one_is_composite(self, q: int) -> None:
        assert _naive_prime(q)
        assert not _naive_prime(2 * q + 1)
        assert not is_sophie_germain_prime(q)

    @pytest.mark.parametrize("q", [-3, 0, 1])
    def test_rejects_degenerate_input(self, q: int) -> None:
        assert not is_sophie_germain_prime(q)


class TestSubgroupOrder:
    @pytest.mark.parametrize("p", SAFE_PRIMES)
    def test_it_is_half_of_p_minus_one(self, p: int) -> None:
        q = subgroup_order(p)
        assert 2 * q + 1 == p
        assert _naive_prime(q)

    @pytest.mark.parametrize("p", [6, 100, 2048])
    def test_an_even_modulus_is_refused_not_floored(self, p: int) -> None:
        """
        (p-1)/2 is not an integer for even p, and a floored value is a
        different group's order -- which every subsequent exponentiation would
        then use, silently.
        """
        with pytest.raises(SafePrimeError, match="odd"):
            subgroup_order(p)

    @pytest.mark.parametrize("p", [-1, 0, 1, 3])
    def test_a_tiny_modulus_is_refused(self, p: int) -> None:
        with pytest.raises(SafePrimeError, match="at least 5"):
            subgroup_order(p)


class TestValidateGroup:
    @pytest.mark.parametrize("p", [11, 23, 47, 59, 83])
    def test_accepts_a_safe_group_with_a_subgroup_generator(self, p: int) -> None:
        """
        Any quadratic residue other than 1 generates the prime-order subgroup
        in a safe-prime group, so squaring a full-order element gives a valid g.
        """
        q = (p - 1) // 2
        g = pow(2, 2, p)
        verdict = validate_group(p, g)
        assert verdict.ok, verdict.problems
        assert verdict.subgroup_order == q
        assert bool(verdict) is True

    @pytest.mark.parametrize("p", PRIME_BUT_NOT_SAFE[:6])
    def test_reports_a_prime_that_is_not_safe(self, p: int) -> None:
        verdict = validate_group(p, 2)
        assert not verdict
        assert any("not a safe prime" in problem for problem in verdict.problems)
        assert verdict.subgroup_order is None

    @pytest.mark.parametrize("n", [9, 15, 25, 33])
    def test_reports_a_composite_modulus(self, n: int) -> None:
        verdict = validate_group(n, 2)
        assert not verdict
        assert any("composite" in problem for problem in verdict.problems)

    def test_an_even_modulus_is_reported_not_crashed(self) -> None:
        verdict = validate_group(100, 2)
        assert not verdict
        assert any("even" in problem for problem in verdict.problems)

    @pytest.mark.parametrize("g", [0, 1, 10, 11, 12, -1])
    def test_a_trivial_or_out_of_range_generator_is_reported(self, g: int) -> None:
        """
        g = 1 has order 1 and g = p-1 has order 2, so each generates one or two
        elements and every shared secret lands in that set.
        """
        verdict = validate_group(11, g)
        assert not verdict
        assert any("outside 1 < g < p-1" in problem for problem in verdict.problems)

    def test_a_generator_outside_the_prime_order_subgroup_is_reported(self) -> None:
        """
        g = 2 mod 11 has order 10, not 5. Not itself an attack -- a full-order
        generator is a legitimate choice -- but it means public values need a
        stronger membership test than the subgroup check, so it is named.
        """
        verdict = validate_group(11, 2)
        assert not verdict
        assert any("prime-order subgroup" in problem for problem in verdict.problems)

    def test_it_reports_every_problem_not_only_the_first(self) -> None:
        """
        An operator fixing parameters should learn the whole story in one pass,
        not one problem per attempt.
        """
        verdict = validate_group(21, 1)
        assert len(verdict.problems) >= 2

    def test_a_tiny_modulus_is_refused(self) -> None:
        with pytest.raises(SafePrimeError, match="at least 5"):
            validate_group(3, 2)

    def test_the_verdict_is_frozen(self) -> None:
        verdict = validate_group(11, 4)
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            verdict.ok = False  # type: ignore[misc]

    def test_a_verdict_is_falsy_when_not_ok(self) -> None:
        assert not GroupVerdict(False, None, ("x",))
        assert GroupVerdict(True, 5, ())


class TestPublicValueValidation:
    """
    The check that actually stops the attack.

    Correct parameters do not prevent a peer from *sending* an element of small
    order: validating the group proves what subgroups exist, not what arrived
    on the wire. A deployment with a flawless modulus and no value check is
    still vulnerable, and that is the combination found in the field.
    """

    @pytest.mark.parametrize("p", [11, 23, 47, 59, 83, 107])
    def test_it_accepts_exactly_the_prime_order_subgroup_minus_one(self, p: int) -> None:
        """
        Completeness, by enumeration rather than assertion.

        The accepted set must be the quadratic residues with 1 removed: 1 is in
        the subgroup but a shared secret of 1 is itself a confinement, so it is
        excluded on purpose.
        """
        q = (p - 1) // 2
        subgroup = {pow(y, 2, p) for y in range(1, p)}
        expected = {y for y in subgroup if y != 1}
        actual = {y for y in range(p) if is_valid_public_value(y, p)}
        assert actual == expected
        assert all(pow(y, q, p) == 1 for y in actual)

    @pytest.mark.parametrize("p", [11, 23, 47, 59])
    def test_it_rejects_every_element_of_order_one_or_two(self, p: int) -> None:
        """
        In a safe-prime group these are exactly 1 and p-1, which is why naming
        them is a complete enumeration rather than a heuristic.
        """
        assert not is_valid_public_value(1, p)
        assert not is_valid_public_value(p - 1, p)
        assert not is_valid_public_value(0, p)

    @pytest.mark.parametrize("p", [11, 23, 47])
    def test_it_rejects_a_full_order_element(self, p: int) -> None:
        """
        A non-residue has order 2q. Accepting it leaks one bit of the private
        exponent through the Legendre symbol of the shared secret, which is the
        small end of the same attack.
        """
        non_residues = [y for y in range(2, p - 1) if pow(y, (p - 1) // 2, p) != 1]
        assert non_residues, "a safe prime has non-residues; the corpus is wrong"
        for y in non_residues:
            assert not is_valid_public_value(y, p)

    @pytest.mark.parametrize("p", [4, 100, 3, 0, -11])
    def test_an_unusable_modulus_is_refused(self, p: int) -> None:
        with pytest.raises(SafePrimeError):
            is_valid_public_value(3, p)

    def test_values_at_or_above_p_minus_one_are_rejected(self) -> None:
        """
        No reduction first: a value of p+3 is not "3 mod p" arriving from a
        peer, it is a malformed message, and reducing it would accept a
        wire format the protocol does not permit.
        """
        assert not is_valid_public_value(10, 11)
        assert not is_valid_public_value(11, 11)
        assert not is_valid_public_value(14, 11)


class TestOrderOf:
    @pytest.mark.parametrize("p", [11, 23, 47, 59])
    def test_it_matches_brute_force(self, p: int) -> None:
        for y in range(1, p):
            expected = next(k for k in range(1, p) if pow(y, k, p) == 1)
            assert order_of(y, p) == expected, y

    @pytest.mark.parametrize("p", [11, 23, 47])
    def test_only_the_four_possible_orders_occur(self, p: int) -> None:
        """
        The structural payoff. In a safe-prime group the orders are 1, 2, q and
        2q and nothing else, which is what lets order_of be exact by trying
        four candidates instead of searching a 2048-bit space.
        """
        q = (p - 1) // 2
        assert {order_of(y, p) for y in range(1, p)} == {1, 2, q, 2 * q}

    @pytest.mark.parametrize("p", [11, 23])
    def test_zero_is_not_a_unit(self, p: int) -> None:
        assert order_of(0, p) is None
        assert order_of(p, p) is None

    @pytest.mark.parametrize("p", [13, 17, 19, 29, 9, 15])
    def test_a_non_safe_prime_is_refused_not_answered_wrongly(self, p: int) -> None:
        """
        A bug found by this test, not merely guarded by it.

        The first version documented the safe-prime assumption and did not
        check it, so for p = 13 and y = 3 -- whose true order is 3, which is
        not among (1, 2, q, 2q) = (1, 2, 6, 12) -- it returned 6. A multiple of
        the order, shaped exactly like the order, with nothing anywhere
        reporting a problem. The assumption is now verified before the
        candidates are tried.
        """
        with pytest.raises(SafePrimeError, match="not a safe prime"):
            order_of(3, p)

    def test_the_specific_wrong_answer_that_prompted_the_check(self) -> None:
        """3 has order 3 mod 13, and 3 is not in (1, 2, 6, 12)."""
        assert next(k for k in range(1, 13) if pow(3, k, 13) == 1) == 3
        assert 3 not in (1, 2, 6, 12)
        assert pow(3, 6, 13) == 1, "which is how a multiple got returned as the order"

    @pytest.mark.parametrize("p", [4, 0, -3])
    def test_an_unusable_modulus_is_refused(self, p: int) -> None:
        with pytest.raises(SafePrimeError):
            order_of(2, p)


class TestSmallestFactorDiagnostic:
    # 17 is deliberately absent: p-1 = 16 = 2^4 has no odd factor at all, so
    # this diagnostic correctly returns None for it even though 17 is not a
    # safe prime. Its subgroups are all powers of two, which the public-value
    # check already covers -- a case worth knowing about rather than forcing
    # into the corpus, and the reason the next test exists.
    @pytest.mark.parametrize("p", [13, 19, 31, 37, 43])
    def test_it_finds_the_small_factor_that_makes_p_unsafe(self, p: int) -> None:
        """
        The quantity an attacker wants: a small odd factor r of p-1 is a
        subgroup of order r to confine a secret into.
        """
        factor = smallest_factor_of_p_minus_one(p)
        assert factor is not None
        assert (p - 1) % factor == 0
        assert factor % 2 == 1

    @pytest.mark.parametrize("p", [263, 347, 359, 383])
    def test_a_safe_prime_has_no_small_odd_factor(self, p: int) -> None:
        """
        p-1 = 2q with q prime and larger than the bound, so there is nothing
        small to find -- which is the property the whole module is about.
        """
        assert smallest_factor_of_p_minus_one(p, bound=128) is None

    def test_the_factor_it_returns_is_the_smallest(self) -> None:
        p = 31  # p-1 = 30 = 2 * 3 * 5
        assert smallest_factor_of_p_minus_one(p) == 3

    @pytest.mark.parametrize("p", [17, 257])
    def test_none_does_not_mean_safe(self, p: int) -> None:
        """
        The diagnostic's limit, pinned so nobody reads None as "safe".

        p-1 = 2^k has no odd factor, so this returns None for a modulus that
        is not a safe prime. It answers "is there a small odd subgroup", which
        is one reason a modulus is unsafe, not the question `is_safe_prime`
        answers.
        """
        assert smallest_factor_of_p_minus_one(p) is None
        assert not is_safe_prime(p)

    @pytest.mark.parametrize("bad", [0, 1, 2])
    def test_an_unusable_bound_is_refused(self, bad: int) -> None:
        with pytest.raises(SafePrimeError, match="bound"):
            smallest_factor_of_p_minus_one(13, bound=bad)

    def test_a_tiny_modulus_is_refused(self) -> None:
        with pytest.raises(SafePrimeError, match="at least 5"):
            smallest_factor_of_p_minus_one(3)


class TestTheModuleDoesNotAgreeKeys:
    def test_there_is_no_key_agreement_or_random_source(self) -> None:
        """
        Scope, asserted. This module holds predicates over public parameters;
        it does not exponentiate a secret and does not sample one. A DH
        implementation also has to do the exponentiation in constant time,
        which nothing here does -- so a function that looked like key agreement
        would be a constant-time claim this module cannot make.
        """
        import inspect

        from src.mathcore.numbertheory import safe_primes

        source = inspect.getsource(safe_primes)
        for banned in ("import random", "import secrets", "os.urandom"):
            assert banned not in source

        forbidden = ("shared_secret", "agree", "private", "keypair", "exchange")
        for name, obj in vars(safe_primes).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != safe_primes.__name__:
                continue
            assert not any(token in name.lower() for token in forbidden), name
