"""
The Walsh-Hadamard transform and the S-box metrics it defines.

Owns the ``walsh-hadamard`` registry entry.

The Walsh-Hadamard transform of a Boolean function is the tool that decides
whether a substitution box resists linear cryptanalysis. From its spectrum come
three numbers a cipher designer lives by:

* **nonlinearity** -- the Hamming distance to the nearest affine function, and
  the direct measure of linear-attack resistance. Higher is better; the AES
  S-box sits at 112 for 8 bits.
* **correlation immunity** -- the order up to which output bits are statistically
  independent of subsets of input bits, which governs resistance to
  correlation attacks on stream ciphers.
* **bent-ness** -- a function is bent when its spectrum is flat, the theoretical
  maximum nonlinearity; bent functions exist only for an even number of inputs.

The transform itself is the fast in-place butterfly, ``O(n log n)`` in the
``2^n`` table size, and it is exact integer arithmetic, so every metric here is
computed without a rounding error to hide behind.

Public analysis of a public S-box; constant-time does not apply.

References: Carlet, *Boolean Functions for Cryptography and Coding Theory*;
MacWilliams & Sloane ch. 14; Rothaus, *On bent functions* (1976).
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "correlation_immunity",
    "is_bent",
    "nonlinearity",
    "walsh_hadamard_transform",
]


def _check_power_of_two(length: int) -> int:
    if length < 1 or (length & (length - 1)) != 0:
        raise ValueError(f"length must be a power of two, got {length}")
    return length.bit_length() - 1


def walsh_hadamard_transform(truth_table: Sequence[int]) -> list[int]:
    """
    The Walsh-Hadamard spectrum of a Boolean function.

    ``truth_table`` is the function's output (0/1) at each of ``2^n`` inputs in
    order. The values are mapped to ``+-1`` as ``(-1)^f`` and transformed by the
    in-place Hadamard butterfly, so the result ``W[a]`` is the correlation
    between ``f`` and the linear function ``a . x``. ``W[0]`` counts the balance
    of the function; the largest ``|W[a]|`` is what nonlinearity is read from.

    The table length must be a power of two -- a Boolean function has ``2^n``
    inputs, and any other length is a malformed table. Not constant time.
    """
    _check_power_of_two(len(truth_table))
    if any(v not in (0, 1) for v in truth_table):
        raise ValueError("a truth table holds only 0 and 1")
    spectrum = [1 - 2 * v for v in truth_table]  # 0 -> +1, 1 -> -1
    length = len(spectrum)
    step = 1
    while step < length:
        for start in range(0, length, 2 * step):
            for i in range(start, start + step):
                a, b = spectrum[i], spectrum[i + step]
                spectrum[i] = a + b
                spectrum[i + step] = a - b
        step *= 2
    return spectrum


def nonlinearity(truth_table: Sequence[int]) -> int:
    """
    The nonlinearity of a Boolean function: its distance to the nearest affine
    function.

    Computed as ``2^(n-1) - (1/2) max|W[a]|`` from the spectrum. This is the
    headline S-box metric; for the AES S-box's coordinate functions it is 112.
    Not constant time.
    """
    n = _check_power_of_two(len(truth_table))
    spectrum = walsh_hadamard_transform(truth_table)
    return (1 << (n - 1)) - max(abs(w) for w in spectrum) // 2


def is_bent(truth_table: Sequence[int]) -> bool:
    """
    Whether a function is bent -- its spectrum is flat at ``+-2^(n/2)``.

    Bent functions achieve the maximum possible nonlinearity and exist only for
    an even ``n``; for odd ``n`` this is always ``False``, which the flat-
    spectrum test returns naturally since ``2^(n/2)`` is not an integer there.
    Not constant time.
    """
    n = _check_power_of_two(len(truth_table))
    if n % 2 == 1:
        return False
    target = 1 << (n // 2)
    return all(abs(w) == target for w in walsh_hadamard_transform(truth_table))


def correlation_immunity(truth_table: Sequence[int]) -> int:
    """
    The correlation-immunity order of a Boolean function.

    A function is correlation-immune of order ``m`` when ``W[a] == 0`` for every
    ``a`` whose binary weight is between 1 and ``m``. This returns the largest
    such ``m`` (0 if ``W[a]`` is non-zero for some weight-1 ``a``). A
    correlation-immune, balanced function is *resilient*, the property a stream
    cipher's combining function needs. Not constant time.
    """
    n = _check_power_of_two(len(truth_table))
    spectrum = walsh_hadamard_transform(truth_table)
    for order in range(1, n + 1):
        if any(spectrum[a] != 0 for a in range(len(spectrum)) if bin(a).count("1") == order):
            return order - 1
    return n
