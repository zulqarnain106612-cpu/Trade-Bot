"""
Tests for :mod:`src.mathcore.fields.ntt`.

The exit gate is two statements about the ML-KEM ring: the NTT round-trips, and
transform-domain multiplication matches schoolbook negacyclic convolution on
random inputs. Both are checked here against an oracle -- schoolbook
convolution -- that shares no butterfly logic with the transform, so agreement
is evidence rather than a tautology. The KEM parameter checks are pinned to the
FIPS 203 table, including the under-width-noise case the roadmap calls out.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.fields.ntt import (
    ML_KEM_N,
    ML_KEM_Q,
    intt,
    negacyclic_convolution,
    ntt,
    ntt_multiply,
    validate_kem_parameters,
)


def _random_poly(rng: random.Random) -> list[int]:
    return [rng.randrange(ML_KEM_Q) for _ in range(ML_KEM_N)]


# ---- the exit gate ---------------------------------------------------------


def test_the_transform_round_trips() -> None:
    rng = random.Random("ntt-roundtrip")
    for _ in range(100):
        f = _random_poly(rng)
        assert intt(ntt(f)) == f


def test_transform_domain_product_matches_schoolbook_convolution() -> None:
    rng = random.Random("ntt-convolution")
    for _ in range(50):
        f = _random_poly(rng)
        g = _random_poly(rng)
        assert intt(ntt_multiply(ntt(f), ntt(g))) == negacyclic_convolution(f, g)


def test_the_transform_is_linear() -> None:
    rng = random.Random("ntt-linear")
    f = _random_poly(rng)
    g = _random_poly(rng)
    ff, gg = ntt(f), ntt(g)
    summed = ntt([(a + b) % ML_KEM_Q for a, b in zip(f, g, strict=True)])
    assert summed == [(a + b) % ML_KEM_Q for a, b in zip(ff, gg, strict=True)]


# ---- boundary polynomials --------------------------------------------------


def test_the_zero_polynomial_transforms_to_zero() -> None:
    zero = [0] * ML_KEM_N
    assert ntt(zero) == zero
    assert intt(zero) == zero


def test_convolution_by_one_is_the_identity() -> None:
    rng = random.Random("ntt-identity")
    one = [1] + [0] * (ML_KEM_N - 1)
    f = _random_poly(rng)
    assert intt(ntt_multiply(ntt(f), ntt(one))) == [x % ML_KEM_Q for x in f]


def test_multiplication_by_x_wraps_with_a_sign_flip() -> None:
    """
    X * (sum a_i X^i) = -a_255 + a_0 X + ... in Z_q[X]/(X^256+1). Pins the
    negacyclic wrap directly, the property the whole ring turns on.
    """
    rng = random.Random("ntt-shift")
    f = _random_poly(rng)
    x = [0, 1] + [0] * (ML_KEM_N - 2)
    expected = [(-f[ML_KEM_N - 1]) % ML_KEM_Q] + f[:-1]
    assert intt(ntt_multiply(ntt(f), ntt(x))) == expected


def test_unreduced_coefficients_are_accepted() -> None:
    rng = random.Random("ntt-unreduced")
    f = [rng.randrange(ML_KEM_Q) + ML_KEM_Q * rng.randint(-2, 2) for _ in range(ML_KEM_N)]
    reduced = [x % ML_KEM_Q for x in f]
    assert ntt(f) == ntt(reduced)


def test_wrong_length_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="256 coefficients"):
        ntt([1, 2, 3])
    with pytest.raises(ValueError, match="256 coefficients"):
        intt([0] * 255)
    with pytest.raises(ValueError, match="256 coefficients"):
        ntt_multiply([0] * 256, [0] * 10)


# ---- FIPS 203 parameter sets -----------------------------------------------


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("ML-KEM-512", {"k": 2, "eta1": 3, "eta2": 2, "du": 10, "dv": 4}),
        ("ML-KEM-768", {"k": 3, "eta1": 2, "eta2": 2, "du": 10, "dv": 4}),
        ("ML-KEM-1024", {"k": 4, "eta1": 2, "eta2": 2, "du": 11, "dv": 5}),
    ],
)
def test_the_standard_parameter_sets_validate(name: str, params: dict) -> None:
    validate_kem_parameters(name, params)  # must not raise
    validate_kem_parameters(name, {**params, "n": 256, "q": 3329})


def test_an_under_width_noise_parameter_is_flagged_with_a_reason() -> None:
    """
    The roadmap's named case: eta1 narrowed below the standard weakens the
    scheme while functional tests still pass, so the check must catch it and
    say why.
    """
    with pytest.raises(ValueError, match="narrows the noise distribution"):
        validate_kem_parameters("ML-KEM-768", {"k": 3, "eta1": 1, "eta2": 2, "du": 10, "dv": 4})


def test_a_wrong_modulus_or_degree_is_rejected() -> None:
    good = {"k": 3, "eta1": 2, "eta2": 2, "du": 10, "dv": 4}
    with pytest.raises(ValueError, match="q must be 3329"):
        validate_kem_parameters("ML-KEM-768", {**good, "q": 3331})
    with pytest.raises(ValueError, match="n must be 256"):
        validate_kem_parameters("ML-KEM-768", {**good, "n": 512})


def test_a_wrong_rank_is_rejected() -> None:
    with pytest.raises(ValueError, match="k must be 3"):
        validate_kem_parameters("ML-KEM-768", {"k": 2, "eta1": 2, "eta2": 2, "du": 10, "dv": 4})


def test_a_missing_parameter_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing parameter"):
        validate_kem_parameters("ML-KEM-768", {"k": 3, "eta1": 2, "eta2": 2, "du": 10})


def test_an_unknown_parameter_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown parameter set"):
        validate_kem_parameters("ML-KEM-2048", {"k": 8})


def test_the_higher_compression_of_ml_kem_1024_is_required() -> None:
    """1024 uses du=11, dv=5; passing the 768 widths must fail."""
    with pytest.raises(ValueError, match="du must be 11"):
        validate_kem_parameters("ML-KEM-1024", {"k": 4, "eta1": 2, "eta2": 2, "du": 10, "dv": 5})
