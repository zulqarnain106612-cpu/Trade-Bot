"""
The Number-Theoretic Transform in the ML-KEM ring, and KEM parameter checks.

Owns the ``ntt`` registry entry and supports ``lattices-module-algebra`` (the
ring these polynomials live in is the module-LWE base ring).

ML-KEM (FIPS 203) does its polynomial multiplication in
``Z_q[X] / (X^256 + 1)`` with ``q = 3329``, and it does it fast because that
ring splits under the NTT. But it splits *incompletely*: ``q - 1 = 3328 =
2^8 * 13`` is divisible by 256 but not by 512, so ``X^256 + 1`` factors into
128 quadratics rather than 256 linear terms. The NTT therefore maps a degree-255
polynomial to 128 elements of ``F_q[X]/(X^2 - zeta^i)`` -- pairs, not points --
and multiplication in the transform domain is 128 little degree-1 products, not
128 scalar products. Getting that base case right is the whole subtlety, and it
is why this module tests the transform against schoolbook negacyclic
convolution rather than trusting the butterflies.

``zeta = 17`` is the primitive 256th root of unity mod 3329 that FIPS 203 fixes,
and the twiddle factors are its powers in bit-reversed order.

This is arithmetic on public lattice data (public keys, ciphertexts), so
constant-time is not this module's concern -- but note that a real ML-KEM
implementation's NTT *is* on a hot constant-time path, and this reference is
not that. It exists to check correctness and parameters, not to ship in a
handshake.

References: FIPS 203 sec. 4.3 and algorithms 9-12; Cooley-Tukey; the Kyber
submission's NTT description.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "ML_KEM_N",
    "ML_KEM_Q",
    "ZETA",
    "base_case_multiply",
    "intt",
    "negacyclic_convolution",
    "ntt",
    "ntt_multiply",
    "validate_kem_parameters",
]

ML_KEM_N = 256
ML_KEM_Q = 3329
ZETA = 17  # primitive 256th root of unity mod 3329, per FIPS 203


def _bit_reverse_7(i: int) -> int:
    """Reverse the low 7 bits of ``i`` (0..127), the FIPS 203 twiddle order."""
    result = 0
    for bit in range(7):
        result |= ((i >> bit) & 1) << (6 - bit)
    return result


# zeta^bitrev7(i) mod q, for i in 0..127 -- the NTT twiddle factors.
_ZETAS = [pow(ZETA, _bit_reverse_7(i), ML_KEM_Q) for i in range(128)]


def _check_polynomial(f: Sequence[int], name: str) -> None:
    if len(f) != ML_KEM_N:
        raise ValueError(f"{name} must have {ML_KEM_N} coefficients, got {len(f)}")


def ntt(f: Sequence[int]) -> list[int]:
    """
    Forward NTT of a 256-coefficient polynomial, per FIPS 203 Algorithm 9.

    Returns the transform as 256 integers, read as 128 coefficient *pairs*: the
    ``i``-th pair is the image in ``F_q[X]/(X^2 - zeta^(2 bitrev7(i) + 1))``.
    Input coefficients are reduced mod ``q`` first. Not constant time.
    """
    _check_polynomial(f, "f")
    a = [x % ML_KEM_Q for x in f]
    k = 1
    length = 128
    while length >= 2:
        start = 0
        while start < ML_KEM_N:
            zeta = _ZETAS[k]
            k += 1
            for j in range(start, start + length):
                t = (zeta * a[j + length]) % ML_KEM_Q
                a[j + length] = (a[j] - t) % ML_KEM_Q
                a[j] = (a[j] + t) % ML_KEM_Q
            start += 2 * length
        length //= 2
    return a


def intt(f_hat: Sequence[int]) -> list[int]:
    """
    Inverse NTT, per FIPS 203 Algorithm 10; the exact inverse of :func:`ntt`.

    The final scaling by ``128^-1 mod q`` (FIPS 203's factor 3303) is what makes
    ``intt(ntt(f)) == f`` rather than ``128 f``. Not constant time.
    """
    _check_polynomial(f_hat, "f_hat")
    a = [x % ML_KEM_Q for x in f_hat]
    k = 127
    length = 2
    while length <= 128:
        start = 0
        while start < ML_KEM_N:
            zeta = _ZETAS[k]
            k -= 1
            for j in range(start, start + length):
                t = a[j]
                a[j] = (t + a[j + length]) % ML_KEM_Q
                a[j + length] = (zeta * (a[j + length] - t)) % ML_KEM_Q
            start += 2 * length
        length *= 2
    inv_128 = pow(128, -1, ML_KEM_Q)
    return [(x * inv_128) % ML_KEM_Q for x in a]


def base_case_multiply(a0: int, a1: int, b0: int, b1: int, gamma: int) -> tuple[int, int]:
    """
    Multiply two linear polynomials in ``F_q[X]/(X^2 - gamma)``.

    ``(a0 + a1 X)(b0 + b1 X) = (a0 b0 + a1 b1 gamma) + (a0 b1 + a1 b0) X`` once
    ``X^2`` is reduced to ``gamma``. FIPS 203 Algorithm 12. Not constant time.
    """
    c0 = (a0 * b0 + a1 * b1 % ML_KEM_Q * gamma) % ML_KEM_Q
    c1 = (a0 * b1 + a1 * b0) % ML_KEM_Q
    return c0, c1


def ntt_multiply(f_hat: Sequence[int], g_hat: Sequence[int]) -> list[int]:
    """
    Multiply two polynomials in the NTT domain, per FIPS 203 Algorithm 11.

    Each of the 128 coefficient pairs is multiplied in its own quadratic ring
    ``F_q[X]/(X^2 - zeta^(2 bitrev7(i) + 1))``. The result is again in the NTT
    domain; apply :func:`intt` to return to coefficients. Not constant time.
    """
    _check_polynomial(f_hat, "f_hat")
    _check_polynomial(g_hat, "g_hat")
    result = [0] * ML_KEM_N
    for i in range(128):
        gamma = pow(ZETA, 2 * _bit_reverse_7(i) + 1, ML_KEM_Q)
        c0, c1 = base_case_multiply(
            f_hat[2 * i], f_hat[2 * i + 1], g_hat[2 * i], g_hat[2 * i + 1], gamma
        )
        result[2 * i] = c0
        result[2 * i + 1] = c1
    return result


def negacyclic_convolution(f: Sequence[int], g: Sequence[int]) -> list[int]:
    """
    Multiply two polynomials in ``Z_q[X]/(X^256 + 1)`` the schoolbook way.

    The independent oracle for the NTT: ``X^256 = -1``, so a product term of
    degree ``>= 256`` wraps around with a sign flip. Quadratic in ``n`` and
    used only in tests -- the NTT exists precisely to avoid this cost, so if the
    two ever disagree it is the transform that is wrong. Not constant time.
    """
    _check_polynomial(f, "f")
    _check_polynomial(g, "g")
    result = [0] * ML_KEM_N
    for i in range(ML_KEM_N):
        if f[i] == 0:
            continue
        for j in range(ML_KEM_N):
            product = f[i] * g[j]
            k = i + j
            if k < ML_KEM_N:
                result[k] = (result[k] + product) % ML_KEM_Q
            else:
                result[k - ML_KEM_N] = (result[k - ML_KEM_N] - product) % ML_KEM_Q
    return result


# FIPS 203 Table 2: the three parameter sets.
_ML_KEM_PARAMETER_SETS = {
    "ML-KEM-512": {"k": 2, "eta1": 3, "eta2": 2, "du": 10, "dv": 4},
    "ML-KEM-768": {"k": 3, "eta1": 2, "eta2": 2, "du": 10, "dv": 4},
    "ML-KEM-1024": {"k": 4, "eta1": 2, "eta2": 2, "du": 11, "dv": 5},
}


def validate_kem_parameters(name: str, parameters: dict[str, int]) -> None:
    """
    Check ``parameters`` against the FIPS 203 set named ``name``.

    Every ML-KEM set shares ``n = 256`` and ``q = 3329`` and varies only in the
    module rank ``k`` and the noise/compression widths. This raises with the
    specific mismatched field if the supplied parameters do not match the
    standard, and -- the part that matters for security -- it flags a noise
    parameter that has been narrowed below the standard, because an
    under-width Gaussian is a real and quiet way to weaken a lattice scheme
    while every functional test still passes.

    An unknown ``name`` is rejected rather than passed. Not constant time.
    """
    if name not in _ML_KEM_PARAMETER_SETS:
        raise ValueError(
            f"unknown parameter set {name!r}; known sets are {sorted(_ML_KEM_PARAMETER_SETS)}"
        )
    expected = _ML_KEM_PARAMETER_SETS[name]

    if parameters.get("n", ML_KEM_N) != ML_KEM_N:
        raise ValueError(f"{name}: n must be {ML_KEM_N}, got {parameters.get('n')}")
    if parameters.get("q", ML_KEM_Q) != ML_KEM_Q:
        raise ValueError(f"{name}: q must be {ML_KEM_Q}, got {parameters.get('q')}")

    for field, want in expected.items():
        got = parameters.get(field)
        if got is None:
            raise ValueError(f"{name}: missing parameter {field!r}")
        if got != want:
            detail = ""
            if field in ("eta1", "eta2") and got < want:
                detail = (
                    " -- this narrows the noise distribution below the standard, "
                    "which weakens the scheme while functional tests still pass"
                )
            raise ValueError(f"{name}: {field} must be {want}, got {got}{detail}")
