"""
Differential and property tests for :mod:`src.mathcore.fields.prime_field`.

The registry's stated risk for ``pseudo-mersenne-primes`` is a reduction that
is correct on random input and wrong on a carry edge. So random input alone is
not the test: every strategy is compared against CPython's ``%`` on the
boundary values around ``p``, ``2**k`` and ``p**2`` as well as on a random
sweep, and the three strategies are compared against each other.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.fields.prime_field import (
    CURVE25519_P,
    NIST_P256_P,
    SECP256K1_P,
    PrimeField,
    PseudoMersenne,
    Reduction,
    as_pseudo_mersenne,
)

SMALL_P = 257
MERSENNE31 = (1 << 31) - 1
CURVE_PRIMES = (SECP256K1_P, CURVE25519_P, NIST_P256_P)
ALL_PRIMES = (SMALL_P, MERSENNE31, *CURVE_PRIMES)


def _strategies(p: int) -> list[Reduction]:
    usable = [Reduction.PYTHON, Reduction.BARRETT, Reduction.MONTGOMERY]
    if as_pseudo_mersenne(p) is not None:
        usable.append(Reduction.PSEUDO_MERSENNE)
    return usable


def _boundaries(p: int) -> list[int]:
    k = p.bit_length()
    seeds = [0, 1, 2, p - 2, p - 1, p, p + 1, p + 2, 1 << (k - 1), 1 << k, (1 << k) + 1]
    seeds += [p * p - 1, p * p, p * p + 1, (1 << (2 * k)) - 1]
    seeds += [p * i + d for i in (1, 2, 3, 7) for d in (-1, 0, 1)]
    return [x for x in seeds if x >= 0]


# ---- known answers ---------------------------------------------------------


def test_curve_primes_match_their_published_values() -> None:
    """SEC 2 v2 and RFC 7748 give these constants explicitly."""
    assert (
        int("fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f", 16) == SECP256K1_P
    )
    assert (
        int("7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed", 16) == CURVE25519_P
    )
    assert (
        int("ffffffff00000001000000000000000000000000ffffffffffffffffffffffff", 16) == NIST_P256_P
    )


@pytest.mark.parametrize("p", (MERSENNE31, SECP256K1_P, CURVE25519_P))
def test_pseudo_mersenne_primes_are_recognised(p: int) -> None:
    shape = as_pseudo_mersenne(p)
    assert shape is not None
    assert shape.prime == p


def test_the_recognised_shapes_are_the_published_ones() -> None:
    assert as_pseudo_mersenne(SECP256K1_P) == PseudoMersenne(k=256, c=(1 << 32) + 977)
    assert as_pseudo_mersenne(CURVE25519_P) == PseudoMersenne(k=255, c=19)


def test_p256_is_solinas_not_pseudo_mersenne() -> None:
    """
    P-256's ``c`` is about ``2**224``, far past the ``2**(k // 2)`` bound. It
    reduces by a Solinas limb recombination, which is a different algorithm
    and not yet built -- so the shape query must say ``None`` rather than hand
    back a fold that would not converge.
    """
    assert as_pseudo_mersenne(NIST_P256_P) is None


def test_a_small_prime_without_the_shape_is_not_claimed() -> None:
    assert as_pseudo_mersenne(SMALL_P) is None


def test_a_prime_without_the_shape_is_rejected_not_approximated() -> None:
    """
    ``c`` must stay below ``2**(k // 2)``; a prime near the middle of its
    bit range has a huge ``c`` and gets ``None`` rather than a shape whose
    fold would not converge usefully.
    """
    assert as_pseudo_mersenne((1 << 64) - (1 << 40)) is None
    assert as_pseudo_mersenne(2) is None
    assert as_pseudo_mersenne(1) is None
    assert as_pseudo_mersenne(1 << 32) is None


def test_pseudo_mersenne_reduce_rejects_negative_input() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        PseudoMersenne(k=255, c=19).reduce(-1)


# ---- construction ----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 1, 2, -7])
def test_modulus_below_three_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        PrimeField(bad)


def test_even_modulus_is_rejected() -> None:
    with pytest.raises(ValueError, match="odd"):
        PrimeField(1 << 32)


def test_bool_is_not_an_acceptable_modulus() -> None:
    """``True == 1`` would otherwise slip past the range check as a modulus."""
    with pytest.raises(TypeError):
        PrimeField(True)


def test_non_integer_modulus_is_rejected() -> None:
    with pytest.raises(TypeError):
        PrimeField(257.0)  # type: ignore[arg-type]


def test_non_enum_strategy_is_rejected() -> None:
    with pytest.raises(TypeError):
        PrimeField(SMALL_P, "barrett")  # type: ignore[arg-type]


def test_pseudo_mersenne_strategy_refuses_a_modulus_without_the_shape() -> None:
    with pytest.raises(ValueError, match="not of the form"):
        PrimeField((1 << 64) - (1 << 40) + 1, Reduction.PSEUDO_MERSENNE)


def test_repr_names_the_modulus_and_strategy() -> None:
    assert repr(PrimeField(SMALL_P, Reduction.BARRETT)) == ("PrimeField(p=257, reduction=barrett)")


def test_accessors_report_the_configured_shape() -> None:
    field = PrimeField(CURVE25519_P, Reduction.MONTGOMERY)
    assert field.reduction is Reduction.MONTGOMERY
    assert field.pseudo_mersenne == PseudoMersenne(k=255, c=19)
    assert field.montgomery_bits == 256


# ---- reduction: differential against CPython -------------------------------


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_reduction_matches_python_on_boundary_values(p: int) -> None:
    for strategy in _strategies(p):
        field = PrimeField(p, strategy)
        for x in _boundaries(p):
            assert field.reduce(x) == x % p, (strategy, x)


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_reduction_matches_python_on_negative_values(p: int) -> None:
    for strategy in _strategies(p):
        field = PrimeField(p, strategy)
        for x in (-1, -p, -p - 1, -(p * p) - 1):
            assert field.reduce(x) == x % p, (strategy, x)


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_reduction_matches_python_on_a_random_sweep(p: int) -> None:
    rng = random.Random(f"reduce-{p}")
    bound = 1 << (2 * p.bit_length())
    for strategy in _strategies(p):
        field = PrimeField(p, strategy)
        for _ in range(2000):
            x = rng.randrange(bound)
            assert field.reduce(x) == x % p, (strategy, x)


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_barrett_stays_correct_above_its_proven_bound(p: int) -> None:
    """
    Past ``2**(2k)`` :meth:`reduce` must fall back rather than extrapolate.
    """
    field = PrimeField(p, Reduction.BARRETT)
    huge = (1 << (3 * p.bit_length())) + 12345
    assert field.reduce(huge) == huge % p


# ---- field axioms ----------------------------------------------------------


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_arithmetic_matches_python_on_a_random_sweep(p: int) -> None:
    rng = random.Random(f"arith-{p}")
    for strategy in _strategies(p):
        field = PrimeField(p, strategy)
        for _ in range(500):
            a = rng.randrange(p)
            b = rng.randrange(p)
            assert field.add(a, b) == (a + b) % p
            assert field.sub(a, b) == (a - b) % p
            assert field.neg(a) == (-a) % p
            assert field.mul(a, b) == (a * b) % p
            assert field.square(a) == (a * a) % p


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_inverse_inverts(p: int) -> None:
    rng = random.Random(f"inv-{p}")
    field = PrimeField(p)
    for _ in range(200):
        a = rng.randrange(1, p)
        assert field.mul(a, field.inv(a)) == 1
        assert field.div(a, a) == 1


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_fermat_little_theorem_holds(p: int) -> None:
    rng = random.Random(f"fermat-{p}")
    field = PrimeField(p)
    for _ in range(50):
        a = rng.randrange(1, p)
        assert field.pow(a, p - 1) == 1


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_negative_exponent_inverts_first(p: int) -> None:
    field = PrimeField(p)
    assert field.pow(3, -1) == field.inv(3)
    assert field.mul(field.pow(3, -5), field.pow(3, 5)) == 1


def test_unreduced_operands_are_accepted() -> None:
    """Callers hand in raw integers; every entry point reduces first."""
    field = PrimeField(SMALL_P)
    assert field.add(SMALL_P + 3, -1) == 2
    assert field.mul(SMALL_P * 4 + 5, SMALL_P + 6) == 30
    assert field.pow(SMALL_P + 2, 8) == pow(2, 8, SMALL_P)
    assert field.inv(SMALL_P + 3) == field.inv(3)


def test_inverting_zero_raises_rather_than_returning_zero() -> None:
    field = PrimeField(SMALL_P)
    with pytest.raises(ZeroDivisionError):
        field.inv(0)
    with pytest.raises(ZeroDivisionError):
        field.inv(SMALL_P)
    with pytest.raises(ZeroDivisionError):
        field.div(1, 0)


# ---- Montgomery form -------------------------------------------------------


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_montgomery_round_trips(p: int) -> None:
    rng = random.Random(f"mont-{p}")
    field = PrimeField(p, Reduction.MONTGOMERY)
    for x in (0, 1, p - 1, *(rng.randrange(p) for _ in range(200))):
        assert field.from_montgomery(field.to_montgomery(x)) == x


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_montgomery_multiplication_is_multiplication(p: int) -> None:
    rng = random.Random(f"montmul-{p}")
    field = PrimeField(p, Reduction.MONTGOMERY)
    for _ in range(200):
        a = rng.randrange(p)
        b = rng.randrange(p)
        product = field.mont_mul(field.to_montgomery(a), field.to_montgomery(b))
        assert field.from_montgomery(product) == (a * b) % p


@pytest.mark.parametrize("p", ALL_PRIMES)
def test_montgomery_results_are_reduced(p: int) -> None:
    """A congruent-but-unreduced result would compare unequal later on."""
    rng = random.Random(f"montred-{p}")
    field = PrimeField(p, Reduction.MONTGOMERY)
    for _ in range(200):
        a = rng.randrange(p)
        b = rng.randrange(p)
        assert 0 <= field.mont_mul(a, b) < p


@pytest.mark.parametrize("p", (SMALL_P, CURVE25519_P))
def test_mont_reduce_rejects_input_outside_its_precondition(p: int) -> None:
    field = PrimeField(p, Reduction.MONTGOMERY)
    with pytest.raises(ValueError, match="0 <= t < p"):
        field.mont_reduce(p << field.montgomery_bits)
    with pytest.raises(ValueError, match="0 <= t < p"):
        field.mont_reduce(-1)


@pytest.mark.parametrize("p", (SMALL_P, CURVE25519_P))
def test_mont_mul_rejects_unreduced_operands(p: int) -> None:
    field = PrimeField(p, Reduction.MONTGOMERY)
    with pytest.raises(ValueError, match="reduced into"):
        field.mont_mul(p, 1)
    with pytest.raises(ValueError, match="reduced into"):
        field.mont_mul(1, -1)


def test_montgomery_radix_is_a_whole_number_of_64_bit_limbs() -> None:
    assert PrimeField(SMALL_P).montgomery_bits == 64
    assert PrimeField(MERSENNE31).montgomery_bits == 64
    assert PrimeField(SECP256K1_P).montgomery_bits == 256
    assert PrimeField(CURVE25519_P).montgomery_bits == 256
