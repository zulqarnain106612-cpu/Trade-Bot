"""
Tests for :mod:`src.mathcore.numbertheory.residues`.

Two independent oracles, neither of which is the implementation under test:

* Euler's criterion, ``a**((p-1)/2) mod p``, for the Legendre symbol.
* The defining product ``prod (a/p_i)**e_i`` over the factorisation of ``n``,
  for the Jacobi symbol at a composite modulus -- the case Euler's criterion
  cannot reach and the case a BPSW primality test actually calls.

Square roots are checked by squaring them back, and non-residues are checked to
return ``None`` rather than a value that squares to something else.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.numbertheory.residues import (
    is_quadratic_residue,
    jacobi_symbol,
    legendre_symbol,
    sqrt_mod_prime,
)

SECP256K1_P = (1 << 256) - (1 << 32) - 977  # 3 mod 4
CURVE25519_P = (1 << 255) - 19  # 3 mod 4
P_1_MOD_4 = (1 << 31) - 19  # 2147483629, prime, 1 mod 4
SMALL_PRIMES_3MOD4 = (3, 7, 11, 19, 23, 31, 43, 127)
SMALL_PRIMES_1MOD4 = (5, 13, 17, 29, 37, 41, 97, 257)
SMALL_PRIMES = SMALL_PRIMES_3MOD4 + SMALL_PRIMES_1MOD4
BIG_PRIMES = (SECP256K1_P, CURVE25519_P, P_1_MOD_4)


def _euler(a: int, p: int) -> int:
    """Legendre symbol by Euler's criterion, in ``{-1, 0, 1}``."""
    if a % p == 0:
        return 0
    return 1 if pow(a, (p - 1) // 2, p) == 1 else -1


def _factorise(n: int) -> list[int]:
    """Prime factors of a small ``n``, with multiplicity."""
    factors = []
    d = 2
    while d * d <= n:
        while n % d == 0:
            factors.append(d)
            n //= d
        d += 1
    if n > 1:
        factors.append(n)
    return factors


def _jacobi_by_definition(a: int, n: int) -> int:
    """``(a/n)`` as the product of Legendre symbols over ``n``'s factors."""
    result = 1
    for q in _factorise(n):
        result *= _euler(a, q)
    return result


# ---- Jacobi and Legendre ---------------------------------------------------


@pytest.mark.parametrize("p", SMALL_PRIMES)
def test_legendre_matches_eulers_criterion_exhaustively(p: int) -> None:
    for a in range(-p, 2 * p + 1):
        assert legendre_symbol(a, p) == _euler(a, p), (a, p)


@pytest.mark.parametrize("p", BIG_PRIMES)
def test_legendre_matches_eulers_criterion_on_large_primes(p: int) -> None:
    rng = random.Random(f"legendre-{p}")
    for a in (0, 1, p - 1, p, *(rng.randrange(p) for _ in range(64))):
        assert legendre_symbol(a, p) == _euler(a, p), (a, p)


def test_jacobi_matches_the_definition_on_every_odd_modulus_below_300() -> None:
    """The composite case, which Euler's criterion does not cover."""
    for n in range(1, 300, 2):
        for a in range(-2 * n, 2 * n + 1):
            assert jacobi_symbol(a, n) == _jacobi_by_definition(a, n), (a, n)


def test_jacobi_is_zero_exactly_when_the_arguments_share_a_factor() -> None:
    import math

    for n in range(1, 200, 2):
        for a in range(0, 3 * n):
            assert (jacobi_symbol(a, n) == 0) == (math.gcd(a, n) > 1), (a, n)


def test_jacobi_of_modulus_one_is_one() -> None:
    """``(a/1) = 1`` for every ``a``, including zero and negatives."""
    for a in (-5, 0, 1, 7):
        assert jacobi_symbol(a, 1) == 1


def test_jacobi_is_periodic_in_its_first_argument() -> None:
    rng = random.Random("periodic")
    for _ in range(200):
        n = rng.randrange(1, 5000) | 1
        a = rng.randrange(-5000, 5000)
        assert jacobi_symbol(a, n) == jacobi_symbol(a + n, n)


def test_jacobi_one_does_not_promise_a_square_at_a_composite_modulus() -> None:
    """
    ``(2/15) = 1`` while 2 is a non-residue mod 15. Documented, and asserted so
    that a future "simplification" into :func:`is_quadratic_residue` fails.
    """
    assert jacobi_symbol(2, 15) == 1
    assert not any(r * r % 15 == 2 for r in range(15))


@pytest.mark.parametrize("bad", [0, -1, 2, 4, -3])
def test_jacobi_rejects_an_even_or_non_positive_modulus(bad: int) -> None:
    with pytest.raises(ValueError, match="odd n >= 1"):
        jacobi_symbol(1, bad)


def test_jacobi_rejects_non_integer_arguments() -> None:
    with pytest.raises(TypeError):
        jacobi_symbol(1, True)
    with pytest.raises(TypeError):
        jacobi_symbol(True, 7)
    with pytest.raises(TypeError):
        jacobi_symbol(1.0, 7)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [2, 1, 0, -7])
def test_legendre_rejects_a_modulus_below_three(bad: int) -> None:
    with pytest.raises(ValueError):
        legendre_symbol(1, bad)


# ---- residuosity -----------------------------------------------------------


@pytest.mark.parametrize("p", SMALL_PRIMES)
def test_is_quadratic_residue_agrees_with_brute_force(p: int) -> None:
    squares = {r * r % p for r in range(1, p)}
    for a in range(p):
        assert is_quadratic_residue(a, p) == (a in squares and a != 0), (a, p)


@pytest.mark.parametrize("p", SMALL_PRIMES)
def test_zero_is_not_reported_as_a_residue(p: int) -> None:
    assert not is_quadratic_residue(0, p)
    assert not is_quadratic_residue(p, p)


# ---- square roots ----------------------------------------------------------


@pytest.mark.parametrize("p", SMALL_PRIMES)
def test_square_roots_are_exhaustively_correct_for_small_primes(p: int) -> None:
    squares = {r * r % p for r in range(p)}
    for a in range(p):
        root = sqrt_mod_prime(a, p)
        if a in squares:
            assert root is not None, (a, p)
            assert root * root % p == a, (a, p)
        else:
            assert root is None, (a, p)


@pytest.mark.parametrize("p", BIG_PRIMES)
def test_square_roots_square_back_on_large_primes(p: int) -> None:
    """Covers both the ``p == 3 (mod 4)`` shortcut and full Tonelli-Shanks."""
    rng = random.Random(f"sqrt-{p}")
    found = 0
    for _ in range(64):
        a = rng.randrange(p)
        root = sqrt_mod_prime(a, p)
        if root is None:
            assert not is_quadratic_residue(a, p)
            continue
        found += 1
        assert root * root % p == a
    assert found > 0, "the sweep produced no residues; the test proved nothing"


@pytest.mark.parametrize("p", BIG_PRIMES)
def test_every_square_has_a_root(p: int) -> None:
    rng = random.Random(f"square-{p}")
    for _ in range(32):
        r = rng.randrange(1, p)
        a = r * r % p
        root = sqrt_mod_prime(a, p)
        assert root in (r, p - r)


@pytest.mark.parametrize("p", SMALL_PRIMES + BIG_PRIMES)
def test_the_root_of_zero_is_zero(p: int) -> None:
    assert sqrt_mod_prime(0, p) == 0
    assert sqrt_mod_prime(p, p) == 0


@pytest.mark.parametrize("p", SMALL_PRIMES)
def test_unreduced_and_negative_inputs_are_reduced_first(p: int) -> None:
    for a in range(-2 * p, 3 * p):
        root = sqrt_mod_prime(a, p)
        if root is not None:
            assert root * root % p == a % p, (a, p)
        else:
            assert not is_quadratic_residue(a, p)


def test_both_roots_are_returned_across_the_two_conventions() -> None:
    """The other root is ``p - r``; callers pick, this does not."""
    root = sqrt_mod_prime(2, 7)
    assert root is not None
    assert {root, 7 - root} == {3, 4}


@pytest.mark.parametrize("bad", [2, 1, 0, -7])
def test_sqrt_rejects_a_modulus_below_three(bad: int) -> None:
    with pytest.raises(ValueError, match="odd prime"):
        sqrt_mod_prime(1, bad)


def test_sqrt_rejects_an_even_modulus() -> None:
    with pytest.raises(ValueError, match="odd prime"):
        sqrt_mod_prime(1, 1 << 32)
