"""
Tests for :mod:`src.mathcore.numbertheory.crt`.

The roadmap's exit gate for this module is one sentence: ``detect_crt_fault``
recovers a factor from a synthetic faulty signature. That is the headline test
below, and it is built the way the attack is -- a real RSA-CRT signature with
one half computed wrongly, then the GCD run against it -- rather than by
asserting the arithmetic identity that makes the attack work.

Everything else here guards the preconditions, because a CRT routine that
answers for non-coprime moduli returns a value satisfying neither congruence.
"""

from __future__ import annotations

import math
import random

import pytest

from src.mathcore.numbertheory.crt import (
    crt,
    crt_pair,
    detect_crt_fault,
    rsa_crt_recombine,
)

# A small but genuine RSA key: p, q prime, e = 65537, d the private exponent.
RSA_P = 1_000_000_007
RSA_Q = 1_000_000_009
RSA_N = RSA_P * RSA_Q
RSA_E = 65537
RSA_D = pow(RSA_E, -1, (RSA_P - 1) * (RSA_Q - 1))


def _sign_crt(message: int, *, fault_in_p_half: bool = False) -> int:
    """
    An RSA-CRT signature, optionally with one half deliberately wrong.

    The fault is a single flipped bit in ``m1``, which is what a glitched
    multiplication or a bad RAM cell produces -- not a random value, so the
    test cannot pass by accident of the fault being obviously malformed.
    """
    m1 = pow(message, RSA_D % (RSA_P - 1), RSA_P)
    m2 = pow(message, RSA_D % (RSA_Q - 1), RSA_Q)
    if fault_in_p_half:
        m1 ^= 1
    return rsa_crt_recombine(m1, m2, RSA_P, RSA_Q)


# ---- the exit gate ---------------------------------------------------------


def test_a_faulty_crt_signature_gives_up_a_factor_of_the_modulus() -> None:
    faulty = _sign_crt(42, fault_in_p_half=True)
    factor = detect_crt_fault(signature=faulty, message=42, public_exponent=RSA_E, modulus=RSA_N)
    assert factor in (RSA_P, RSA_Q)
    assert RSA_N % factor == 0


def test_the_recovered_factor_is_the_prime_whose_half_was_not_faulted() -> None:
    """
    The fault was in the ``p`` half, so ``s**e - m`` is divisible by ``q`` and
    not by ``p``. Which factor comes back is not incidental -- it identifies
    where the fault happened.
    """
    faulty = _sign_crt(42, fault_in_p_half=True)
    assert (
        detect_crt_fault(signature=faulty, message=42, public_exponent=RSA_E, modulus=RSA_N)
        == RSA_Q
    )


def test_a_correct_signature_yields_no_factor() -> None:
    for message in (0, 1, 2, 42, RSA_N - 1):
        good = _sign_crt(message)
        assert (
            detect_crt_fault(signature=good, message=message, public_exponent=RSA_E, modulus=RSA_N)
            is None
        )


def test_every_single_bit_fault_in_a_half_is_caught() -> None:
    """
    Not one synthetic fault but every one-bit fault in the low 30 bits of the
    ``p`` half. A check that catches most faults is not a check.
    """
    message = 123456789
    m2 = pow(message, RSA_D % (RSA_Q - 1), RSA_Q)
    base_m1 = pow(message, RSA_D % (RSA_P - 1), RSA_P)
    for bit in range(30):
        m1 = base_m1 ^ (1 << bit)
        if m1 >= RSA_P:
            continue
        faulty = rsa_crt_recombine(m1, m2, RSA_P, RSA_Q)
        factor = detect_crt_fault(
            signature=faulty, message=message, public_exponent=RSA_E, modulus=RSA_N
        )
        assert factor == RSA_Q, bit


def test_a_signature_from_the_wrong_key_raises_rather_than_claiming_a_fault() -> None:
    """
    A wrong message or modulus is a different bug from a CRT fault, and
    reporting it as one would send a caller to destroy a key that is fine.
    """
    good = _sign_crt(42)
    with pytest.raises(ValueError, match="different bug"):
        detect_crt_fault(signature=good, message=43, public_exponent=RSA_E, modulus=RSA_N)


def test_detect_crt_fault_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="public exponent"):
        detect_crt_fault(signature=1, message=1, public_exponent=0, modulus=RSA_N)
    with pytest.raises(ValueError, match="modulus"):
        detect_crt_fault(signature=1, message=1, public_exponent=RSA_E, modulus=0)
    with pytest.raises(TypeError):
        detect_crt_fault(signature=1, message=1, public_exponent=RSA_E, modulus=True)


# ---- RSA-CRT recombination -------------------------------------------------


def test_recombination_agrees_with_the_direct_private_operation() -> None:
    rng = random.Random("rsa-crt")
    for _ in range(50):
        message = rng.randrange(RSA_N)
        assert _sign_crt(message) == pow(message, RSA_D, RSA_N)


def test_recombination_satisfies_both_congruences() -> None:
    rng = random.Random("congruences")
    for _ in range(100):
        m1 = rng.randrange(RSA_P)
        m2 = rng.randrange(RSA_Q)
        x = rsa_crt_recombine(m1, m2, RSA_P, RSA_Q)
        assert x % RSA_P == m1
        assert x % RSA_Q == m2
        assert 0 <= x < RSA_N


def test_a_precomputed_q_inv_gives_the_same_answer() -> None:
    q_inv = pow(RSA_Q, -1, RSA_P)
    assert rsa_crt_recombine(3, 5, RSA_P, RSA_Q, q_inv) == rsa_crt_recombine(3, 5, RSA_P, RSA_Q)


def test_a_wrong_q_inv_is_rejected_not_used() -> None:
    """An unchecked q_inv produces output congruent to neither half."""
    with pytest.raises(ValueError, match="q_inv"):
        rsa_crt_recombine(3, 5, RSA_P, RSA_Q, pow(RSA_Q, -1, RSA_P) + 1)


def test_recombination_requires_coprime_factors() -> None:
    with pytest.raises(ValueError, match="coprime"):
        rsa_crt_recombine(1, 1, 15, 21)


@pytest.mark.parametrize("bad", [0, -1])
def test_recombination_rejects_a_non_positive_modulus(bad: int) -> None:
    with pytest.raises(ValueError):
        rsa_crt_recombine(1, 1, bad, 7)
    with pytest.raises(ValueError):
        rsa_crt_recombine(1, 1, 7, bad)


# ---- the general theorem ---------------------------------------------------


def test_crt_pair_solves_both_congruences_exhaustively() -> None:
    for n1 in range(1, 16):
        for n2 in range(1, 16):
            if math.gcd(n1, n2) != 1:
                continue
            for a1 in range(n1):
                for a2 in range(n2):
                    x, modulus = crt_pair(a1, n1, a2, n2)
                    assert modulus == n1 * n2
                    assert 0 <= x < modulus
                    assert x % n1 == a1
                    assert x % n2 == a2


def test_crt_pair_reduces_unreduced_and_negative_residues() -> None:
    x, modulus = crt_pair(-1, 7, 30, 11)
    assert modulus == 77
    assert x % 7 == 6
    assert x % 11 == 8


def test_crt_pair_refuses_non_coprime_moduli() -> None:
    """
    ``x == 1 (mod 4)`` and ``x == 3 (mod 6)`` is solvable, but modulo 12 rather
    than 24 -- a different theorem. Answering it here would return a value
    satisfying neither congruence.
    """
    with pytest.raises(ValueError, match="coprime"):
        crt_pair(1, 4, 3, 6)


def test_crt_folds_a_whole_system() -> None:
    moduli = (3, 5, 7, 11, 13)
    residues = (2, 3, 2, 5, 12)
    x, modulus = crt(residues, moduli)
    assert modulus == math.prod(moduli)
    for a, n in zip(residues, moduli, strict=True):
        assert x % n == a


def test_crt_matches_a_brute_force_search() -> None:
    rng = random.Random("crt-brute")
    moduli = (5, 7, 9, 11)
    product = math.prod(moduli)
    for _ in range(30):
        target = rng.randrange(product)
        residues = tuple(target % n for n in moduli)
        assert crt(residues, moduli) == (target, product)


def test_the_empty_system_is_the_identity() -> None:
    """Every integer satisfies no constraints; ``0 mod 1`` represents that."""
    assert crt((), ()) == (0, 1)


def test_a_repeated_modulus_is_caught_at_the_pair_that_introduces_it() -> None:
    with pytest.raises(ValueError, match="coprime"):
        crt((1, 2), (5, 5))


def test_crt_requires_one_residue_per_modulus() -> None:
    with pytest.raises(ValueError, match="one residue per modulus"):
        crt((1, 2), (3,))


def test_crt_rejects_a_non_integer_modulus() -> None:
    with pytest.raises(TypeError):
        crt_pair(1, 5, 1, True)
