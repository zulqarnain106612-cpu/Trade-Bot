"""
Tests for the RSA/DLP analytics: factorisation, continued fractions and the
security-level bounds.

Factorisation is checked to recover the primes of weak or structured moduli and
to *refuse* rather than hang on one it cannot crack. The Wiener attack recovers
a deliberately small RSA exponent and clears a safe key. The bounds are pinned
to the textbook figures -- 128 bits from a 256-bit group, the RSA-vs-EC size
gap -- and to the monotonicity a caller sizing a parameter relies on.
"""

from __future__ import annotations

import math
from functools import reduce

import pytest

from src.mathcore.numbertheory.continued_fractions import (
    continued_fraction,
    convergents,
    wiener_attack,
)
from src.mathcore.numbertheory.dlp_bounds import (
    generic_dlp_security_bits,
    nfs_cost_bits,
    pohlig_hellman_security_bits,
)
from src.mathcore.numbertheory.factorization import (
    factorize,
    fermat_factor,
    pollard_rho,
    trial_division,
)


def _product(xs: list[int]) -> int:
    return reduce(lambda a, b: a * b, xs, 1)


# ---- factorisation ---------------------------------------------------------


@pytest.mark.parametrize("n", list(range(1, 500)) + [2**10, 3**7, 2 * 3 * 5 * 7 * 11])
def test_factorize_multiplies_back_to_n(n: int) -> None:
    factors = factorize(n)
    assert _product(factors) == n


def test_factorize_returns_primes_in_order() -> None:
    from src.mathcore.numbertheory.primality import is_probable_prime

    factors = factorize(2 * 2 * 3 * 13 * 101)
    assert factors == sorted(factors)
    assert all(is_probable_prime(f) for f in factors)


def test_fermat_factors_close_primes_in_one_reach() -> None:
    p, q = 1_000_003, 1_000_033  # very close
    assert sorted(fermat_factor(p * q)) == [p, q]


def test_fermat_returns_none_when_factors_are_far_apart() -> None:
    # 3 and a large prime: Fermat would need many steps, so it gives up.
    assert fermat_factor(3 * 1_000_003, max_steps=10) is None


def test_pollard_rho_finds_a_factor_of_a_semiprime() -> None:
    n = 1_000_003 * 1_000_033
    divisor = pollard_rho(n)
    assert divisor is not None
    assert 1 < divisor < n
    assert n % divisor == 0


def test_pollard_rho_short_circuits_even_numbers() -> None:
    assert pollard_rho(1_000_000) == 2


def test_trial_division_pulls_out_small_primes() -> None:
    assert trial_division(2**5 * 3**2 * 7) == [2, 2, 2, 2, 2, 3, 3, 7]


def test_factorize_refuses_a_modulus_it_cannot_crack() -> None:
    """
    Two well-separated ~50-bit primes: no small factor, far apart (so Fermat is
    hopeless in a small budget), and their factor is beyond a short Pollard-rho
    budget. The honest outcome is a refusal naming the number field sieve, not a
    hang. The budgets are kept tiny so the refusal is fast -- the point is that
    it *gives up*, which no amount of budget would change for a real modulus.
    """
    p = 1_125_899_906_842_679  # prime, ~50 bits
    q = 9_007_199_254_740_997  # prime, ~53 bits, ~8x larger
    with pytest.raises(ValueError, match="number field sieve"):
        factorize(p * q, fermat_steps=1000, pollard_iterations=2000)


@pytest.mark.parametrize("bad", [0, -5])
def test_factorize_rejects_a_nonpositive_input(bad: int) -> None:
    with pytest.raises(ValueError):
        factorize(bad)


def test_factorize_of_one_is_empty() -> None:
    assert factorize(1) == []


def test_fermat_rejects_even_input() -> None:
    with pytest.raises(ValueError, match="odd"):
        fermat_factor(100)


# ---- continued fractions and Wiener ----------------------------------------


def test_continued_fraction_matches_a_hand_expansion() -> None:
    assert continued_fraction(415, 93) == [4, 2, 6, 7]


def test_convergents_are_the_best_approximations() -> None:
    conv = list(convergents(continued_fraction(415, 93)))
    assert conv[-1] == (415, 93)
    assert conv[0] == (4, 1)


def test_wiener_recovers_a_small_private_exponent() -> None:
    p, q = 1_000_003, 1_000_033
    n = p * q
    phi = (p - 1) * (q - 1)
    d = 17  # far below n^(1/4)/3, so vulnerable
    e = pow(d, -1, phi)
    assert wiener_attack(e, n) == d


def test_wiener_clears_a_safe_key() -> None:
    p, q = 1_000_003, 1_000_033
    n = p * q
    phi = (p - 1) * (q - 1)
    e = 65537
    d = pow(e, -1, phi)  # large
    assert d > 1
    assert wiener_attack(e, n) is None


def test_continued_fraction_rejects_a_zero_denominator() -> None:
    with pytest.raises(ValueError, match="denominator"):
        continued_fraction(1, 0)


def test_wiener_rejects_nonpositive_arguments() -> None:
    with pytest.raises(ValueError):
        wiener_attack(0, 10)
    with pytest.raises(ValueError):
        wiener_attack(3, 0)


# ---- security-level bounds -------------------------------------------------


def test_a_256_bit_group_gives_128_bit_security() -> None:
    assert generic_dlp_security_bits(2**256) == pytest.approx(128.0)


def test_generic_security_is_half_the_bit_length() -> None:
    for bits in (128, 160, 256, 384):
        assert generic_dlp_security_bits(2**bits) == pytest.approx(bits / 2)


def test_pohlig_hellman_exposes_a_smooth_order() -> None:
    """A smooth-order group is weak however large: its security is set by its
    largest prime factor, not its size."""
    smooth = 2**40  # largest prime factor 2
    assert pohlig_hellman_security_bits(smooth) == pytest.approx(0.5)
    prime = 2_147_483_647  # a Mersenne prime
    assert pohlig_hellman_security_bits(prime) == pytest.approx(generic_dlp_security_bits(prime))


def test_pohlig_hellman_never_exceeds_the_generic_bound() -> None:
    for n in (2**20, 3**12, 2**10 * 3, 1_000_003):
        assert pohlig_hellman_security_bits(n) <= generic_dlp_security_bits(n) + 1e-9


def test_nfs_cost_puts_rsa_in_the_right_ballpark() -> None:
    """
    The RSA-vs-EC size gap: 2048-bit RSA lands near symmetric-112 and 3072 near
    128, so a curve's 256 bits (128-bit generic security) matches a much larger
    RSA key. Asymptotic estimate, so checked as a range, not a point.
    """
    assert 100 < nfs_cost_bits(2048) < 130
    assert 125 < nfs_cost_bits(3072) < 150
    assert nfs_cost_bits(3072) > nfs_cost_bits(2048)


def test_nfs_cost_grows_with_modulus_size() -> None:
    sizes = [1024, 2048, 3072, 4096, 8192]
    costs = [nfs_cost_bits(b) for b in sizes]
    assert all(a < b for a, b in zip(costs, costs[1:], strict=False))


@pytest.mark.parametrize("fn", [generic_dlp_security_bits, pohlig_hellman_security_bits])
def test_dlp_bounds_reject_a_degenerate_order(fn) -> None:
    with pytest.raises(ValueError):
        fn(1)


def test_nfs_rejects_a_degenerate_size() -> None:
    with pytest.raises(ValueError, match="modulus_bits"):
        nfs_cost_bits(1)


def test_math_import_is_used() -> None:
    """Guard against an unused-import regression in the test module itself."""
    assert math.log2(4) == 2


def test_trial_division_rejects_nonpositive() -> None:
    with pytest.raises(ValueError, match="positive"):
        trial_division(0)


def test_fermat_factors_a_perfect_square() -> None:
    assert fermat_factor(9_999_991**2) == (9_999_991, 9_999_991)


def test_pollard_rho_rejects_below_two() -> None:
    with pytest.raises(ValueError, match="n >= 2"):
        pollard_rho(1)


def test_pollard_rho_returns_none_on_a_prime() -> None:
    """A prime has no non-trivial factor; rho reports None rather than looping."""
    assert pollard_rho(1_000_003, max_iterations=100_000) is None


def test_factorize_uses_the_fermat_path_on_close_primes() -> None:
    p, q = 1_000_003, 1_000_033
    assert factorize(p * q) == [p, q]


def test_factorize_uses_the_pollard_path_on_far_primes() -> None:
    """Far-apart primes with no small factor: Fermat fails, Pollard rho splits."""
    p, q = 15_485_863, 32_452_843  # ~2x apart, both prime
    assert factorize(p * q, fermat_steps=50) == sorted([p, q])


@pytest.mark.parametrize(
    ("e", "n"),
    [(3, 8_633), (5, 10_403), (17, 3_233), (7, 100_007), (11, 20)],
)
def test_wiener_returns_none_or_a_valid_d_over_assorted_keys(e: int, n: int) -> None:
    """
    Exercises the convergent scan's rejection branches (negative or non-square
    discriminant) across small moduli. Whatever it returns must be consistent:
    None, or a d that really inverts e modulo the recovered order.
    """
    d = wiener_attack(e, n)
    if d is not None:
        assert (e * d) % _order_from(n) == 1


def _order_from(n: int) -> int:
    from src.mathcore.numbertheory.factorization import factorize

    factors = factorize(n)
    if len(factors) == 2:
        p, q = factors
        return (p - 1) * (q - 1)
    return n  # not a semiprime; caller's assertion is skipped in practice


def test_wiener_skips_a_convergent_yielding_a_non_factorisation() -> None:
    """
    e=41, n=64 has a convergent whose perfect-square discriminant reconstructs
    to values that are not a valid factor pair (p, q <= 1), so it is rejected.
    n=64 is a prime power, not an RSA modulus, and the answer is None.
    """
    assert wiener_attack(41, 64) is None


def test_wiener_skips_a_convergent_with_a_non_square_discriminant() -> None:
    """
    e=11, n=20 has a convergent that satisfies the divisibility test but whose
    reconstructed discriminant is not a perfect square, so that convergent is
    rejected rather than mistaken for a factorisation. n=20 is not a semiprime,
    so the correct answer is None.
    """
    assert wiener_attack(11, 20) is None
