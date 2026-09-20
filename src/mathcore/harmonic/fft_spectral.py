"""
The FFT and the spectral view of a side-channel trace.

Owns the ``fft-side-channel`` registry entry.

Power, electromagnetic and timing traces of a device carry the leakage of the
secret it is processing, and that leakage is often clearest in the frequency
domain -- a clock-correlated spike, a periodic key-dependent pattern. The FFT is
what turns a raw trace into that view and makes differential power analysis
tractable, so this module is here for the *defender* profiling their own device,
the same posture as the rest of :mod:`mathcore`.

The transform is radix-2 Cooley-Tukey on complex samples, with a naive DFT kept
alongside as the oracle the tests check it against. :func:`power_spectrum` and
:func:`dominant_frequency` are the two things a first-pass leakage assessment
actually asks for.

Floating-point complex arithmetic on public trace data; constant-time does not
apply, and this profiles hardware rather than running in it.

References: Cooley & Tukey (1965); Mangard, Oswald & Popp, *Power Analysis
Attacks*; Gebotys et al. on EM frequency-domain analysis.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence

__all__ = [
    "dft",
    "dominant_frequency",
    "fft",
    "ifft",
    "power_spectrum",
]


def _check_power_of_two(length: int) -> None:
    if length < 1 or (length & (length - 1)) != 0:
        raise ValueError(f"length must be a power of two, got {length}")


def dft(samples: Sequence[complex]) -> list[complex]:
    """
    The discrete Fourier transform by its definition, ``O(n^2)``.

    The oracle the FFT is tested against: correct by construction and slow by
    construction, which is exactly what a reference comparison wants. Any
    length is accepted. Not constant time.
    """
    n = len(samples)
    if n == 0:
        raise ValueError("cannot transform an empty signal")
    result = []
    for k in range(n):
        acc = 0j
        for t, x in enumerate(samples):
            acc += x * cmath.exp(-2j * math.pi * k * t / n)
        result.append(acc)
    return result


def fft(samples: Sequence[complex]) -> list[complex]:
    """
    The fast Fourier transform, radix-2 Cooley-Tukey.

    Requires a power-of-two length -- the radix-2 split needs it, and padding a
    trace to the next power of two is the caller's decision to make explicitly
    rather than have this pad silently and shift every frequency bin. Matches
    :func:`dft` to floating-point tolerance. Not constant time.
    """
    x = [complex(v) for v in samples]
    n = len(x)
    _check_power_of_two(n)
    if n == 1:
        return x
    even = fft(x[0::2])
    odd = fft(x[1::2])
    result = [0j] * n
    for k in range(n // 2):
        twiddle = cmath.exp(-2j * math.pi * k / n) * odd[k]
        result[k] = even[k] + twiddle
        result[k + n // 2] = even[k] - twiddle
    return result


def ifft(spectrum: Sequence[complex]) -> list[complex]:
    """
    The inverse FFT, the exact inverse of :func:`fft` up to rounding.

    Computed as the conjugate of the forward transform of the conjugate,
    divided by ``n`` -- the standard identity, so the same butterfly serves both
    directions. Not constant time.
    """
    n = len(spectrum)
    _check_power_of_two(n)
    conjugated = [c.conjugate() for c in spectrum]
    transformed = fft(conjugated)
    return [c.conjugate() / n for c in transformed]


def power_spectrum(samples: Sequence[complex]) -> list[float]:
    """
    The power at each frequency bin: ``|FFT|^2``.

    The quantity a leakage assessment reads -- a key-dependent periodicity shows
    as a peak here. Real-valued and non-negative. Not constant time.
    """
    return [abs(c) ** 2 for c in fft(samples)]


def dominant_frequency(samples: Sequence[complex]) -> int:
    """
    The index of the strongest non-DC frequency bin.

    Bin 0 is the DC component -- the trace's mean offset -- which carries no
    periodic leakage and is skipped, because otherwise every trace's answer is
    trivially 0. The returned index is the bin most likely to hold a
    clock-correlated or key-dependent signal. A length-1 signal has no non-DC
    bin and is refused. Not constant time.
    """
    spectrum = power_spectrum(samples)
    if len(spectrum) < 2:
        raise ValueError("need at least two samples to have a non-DC frequency")
    # Only the first half is unique for a real signal, but this accepts complex
    # input, so scan every bin except DC.
    best_index = 1
    best_power = spectrum[1]
    for i in range(2, len(spectrum)):
        if spectrum[i] > best_power:
            best_power = spectrum[i]
            best_index = i
    return best_index
