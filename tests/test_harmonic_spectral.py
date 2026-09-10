"""
Tests for the spectral half of :mod:`src.mathcore.harmonic`.

The Walsh-Hadamard metrics are checked against the AES S-box (nonlinearity 112,
the published figure) and against functions whose properties are known by hand:
a linear function has nonlinearity 0, x1*x2 is bent, a parity function is
correlation-immune to one below its arity. The FFT is checked against the naive
DFT and for round-trip inversion, and the leakage helpers against a signal with
a planted periodicity.
"""

from __future__ import annotations

import math
import random

import pytest

from src.mathcore.fields.binary_field import AES_POLY, BinaryField
from src.mathcore.harmonic.fft_spectral import (
    dft,
    dominant_frequency,
    fft,
    ifft,
    power_spectrum,
)
from src.mathcore.harmonic.walsh_hadamard import (
    correlation_immunity,
    is_bent,
    nonlinearity,
    walsh_hadamard_transform,
)


def _aes_sbox_bit(bit: int) -> list[int]:
    field = BinaryField(AES_POLY)

    def sbox(a: int) -> int:
        b = 0 if a == 0 else field.inv(a)
        out = 0
        for i in range(8):
            v = (
                ((b >> i) & 1)
                ^ ((b >> ((i + 4) % 8)) & 1)
                ^ ((b >> ((i + 5) % 8)) & 1)
                ^ ((b >> ((i + 6) % 8)) & 1)
                ^ ((b >> ((i + 7) % 8)) & 1)
                ^ ((0x63 >> i) & 1)
            )
            out |= v << i
        return out

    return [(sbox(x) >> bit) & 1 for x in range(256)]


# ---- Walsh-Hadamard --------------------------------------------------------


@pytest.mark.parametrize("bit", range(8))
def test_every_aes_sbox_coordinate_has_nonlinearity_112(bit: int) -> None:
    """The published AES figure -- no S-box with a defect reaches it."""
    assert nonlinearity(_aes_sbox_bit(bit)) == 112


def test_a_linear_function_has_zero_nonlinearity() -> None:
    assert nonlinearity([0, 1, 0, 1]) == 0  # f(x) = x0
    assert nonlinearity([0, 0, 1, 1]) == 0  # f(x) = x1


def test_the_and_function_is_bent() -> None:
    assert is_bent([0, 0, 0, 1])  # x0 & x1, n=2
    assert nonlinearity([0, 0, 0, 1]) == 1  # 2^(n-1) - 2^(n/2-1) = 2 - 1


def test_odd_arity_functions_are_never_bent() -> None:
    assert not is_bent([0, 1, 1, 0, 1, 0, 0, 1])  # n=3


def test_a_parity_function_is_correlation_immune() -> None:
    """x0 ^ x1 ^ x2 is correlation-immune of order n-1 = 2."""
    parity = [bin(x).count("1") % 2 for x in range(8)]
    assert correlation_immunity(parity) == 2


def test_a_constant_function_is_maximally_correlation_immune() -> None:
    assert correlation_immunity([0, 0, 0, 0]) == 2


def test_the_transform_of_the_zero_function_is_a_spike() -> None:
    """(-1)^0 everywhere is all +1; its spectrum is 2^n at bin 0, else 0."""
    spectrum = walsh_hadamard_transform([0, 0, 0, 0])
    assert spectrum == [4, 0, 0, 0]


def test_parsevals_identity_holds() -> None:
    """Sum of squared spectrum equals 2^(2n) for any Boolean function."""
    rng = random.Random("parseval")
    tt = [rng.randint(0, 1) for _ in range(16)]
    assert sum(w * w for w in walsh_hadamard_transform(tt)) == 16 * 16


def test_walsh_rejects_a_non_power_of_two_table() -> None:
    with pytest.raises(ValueError, match="power of two"):
        walsh_hadamard_transform([0, 1, 0])


def test_walsh_rejects_non_bit_entries() -> None:
    with pytest.raises(ValueError, match="0 and 1"):
        walsh_hadamard_transform([0, 2, 0, 1])


# ---- FFT -------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 4, 8, 16, 32, 64])
def test_fft_matches_the_naive_dft(n: int) -> None:
    rng = random.Random(f"fft-{n}")
    signal = [complex(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(n)]
    fast = fft(signal)
    slow = dft(signal)
    assert max(abs(a - b) for a, b in zip(fast, slow, strict=True)) < 1e-9


def test_fft_inverts() -> None:
    rng = random.Random("ifft")
    signal = [rng.gauss(0, 1) for _ in range(128)]
    recovered = ifft(fft(signal))
    assert max(abs(complex(a) - b) for a, b in zip(signal, recovered, strict=True)) < 1e-9


def test_a_planted_periodicity_shows_as_the_dominant_frequency() -> None:
    """A cosine at bin 7 dominates the spectrum, DC aside."""
    n = 64
    signal = [math.cos(2 * math.pi * 7 * t / n) for t in range(n)]
    assert dominant_frequency(signal) == 7


def test_dc_offset_is_ignored_by_dominant_frequency() -> None:
    """A large constant offset must not be reported as the leak frequency."""
    n = 32
    signal = [100.0 + math.sin(2 * math.pi * 3 * t / n) for t in range(n)]
    assert dominant_frequency(signal) in (3, n - 3)


def test_power_spectrum_is_real_and_nonnegative() -> None:
    rng = random.Random("power")
    signal = [complex(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(16)]
    spectrum = power_spectrum(signal)
    assert all(isinstance(p, float) and p >= 0 for p in spectrum)


def test_fft_requires_a_power_of_two_length() -> None:
    with pytest.raises(ValueError, match="power of two"):
        fft([1, 2, 3])


def test_dft_rejects_an_empty_signal() -> None:
    with pytest.raises(ValueError, match="empty"):
        dft([])


def test_dominant_frequency_needs_two_samples() -> None:
    with pytest.raises(ValueError, match="two samples"):
        dominant_frequency([5.0])
