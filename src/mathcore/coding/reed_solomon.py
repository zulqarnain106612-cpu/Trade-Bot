"""
Reed-Solomon erasure coding over GF(2^8).

Owns the ``reed-solomon`` registry entry.

Reed-Solomon adds ``2t`` parity symbols to a message so that any ``t`` erased
or corrupted symbols can be recovered. It is what lets a sharded backup survive
lost shards, and the same code underlies QR codes and much of storage. This
module implements the systematic form over GF(2^8) -- the AES field, so the
arithmetic is shared with :mod:`src.mathcore.fields.binary_field` rather than
reimplemented -- with encoding and erasure decoding.

An MDS code meets the Singleton bound with equality: ``n - k`` parity symbols
correct exactly ``n - k`` erasures, no fewer and no more. That is the property
:mod:`src.mathcore.coding.mds` is named for, and Reed-Solomon is the archetypal
MDS code, so the two entries are siblings.

Encoding and decoding are on public coded data; constant-time does not apply.
This is a correctness reference, not a throughput-tuned codec.

References: Reed & Solomon (1960); Lin & Costello, *Error Control Coding*;
Plank, *A Tutorial on Reed-Solomon Coding* (1997).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..fields.binary_field import AES_POLY, BinaryField

__all__ = [
    "ReedSolomon",
]


class ReedSolomon:
    """
    A systematic Reed-Solomon code over GF(2^8) with ``n`` total and ``k``
    message symbols.

    Systematic means the first ``k`` output symbols are the message unchanged
    and the last ``n - k`` are parity, so an uncorrupted message needs no
    decoding at all. The evaluation points are ``1, alpha, alpha^2, ...`` for a
    field generator ``alpha``; a Vandermonde system in those points is what
    makes the code MDS.

    Symbols are integers in ``[0, 256)``. ``n`` must not exceed 255 (the number
    of non-zero field elements) and ``k`` must be positive and below ``n`` --
    the preconditions that keep the evaluation points distinct and leave room
    for at least one parity symbol. No method is constant time.
    """

    __slots__ = ("_field", "_gen_powers", "k", "n")

    def __init__(self, n: int, k: int) -> None:
        if not 0 < k < n:
            raise ValueError(f"require 0 < k < n, got k={k}, n={n}")
        if n > 255:
            raise ValueError(f"n must be <= 255 over GF(2^8); {n} would repeat an evaluation point")
        self.n = n
        self.k = k
        self._field = BinaryField(AES_POLY)
        # Evaluation points 1, g, g^2, ... using the field generator 3 (a known
        # primitive element of GF(2^8) with the AES polynomial).
        generator = 3
        self._gen_powers = [self._field.pow(generator, i) for i in range(n)]

    def encode(self, message: Sequence[int]) -> list[int]:
        """
        Encode ``k`` message symbols into ``n`` code symbols.

        Systematic: the message is evaluated as a polynomial whose coefficients
        are chosen so the first ``k`` code symbols equal the message. Here it is
        done directly -- the message *is* the first ``k`` symbols, and each
        parity symbol is a fixed linear combination of the message under the
        Vandermonde matrix of evaluation points. Not constant time.
        """
        if len(message) != self.k:
            raise ValueError(f"message must have {self.k} symbols, got {len(message)}")
        if any(not 0 <= s < 256 for s in message):
            raise ValueError("every symbol must be a byte in [0, 256)")

        # Interpolate the degree-<k polynomial through (point_i, message_i) for
        # the first k points, then evaluate it at the remaining n-k points.
        points = self._gen_powers[: self.k]
        parity_points = self._gen_powers[self.k : self.n]
        coeffs = self._interpolate(points, list(message))
        parity = [self._eval(coeffs, x) for x in parity_points]
        return list(message) + parity

    def decode(self, received: Sequence[int], erasures: Sequence[int]) -> list[int]:
        """
        Recover the ``k`` message symbols given the code word and known erasure
        positions.

        ``erasures`` lists the indices in ``received`` known to be lost; their
        values are ignored. As long as at least ``k`` symbols survive, the
        message is the unique degree-``<k`` polynomial through any ``k`` of the
        surviving (point, value) pairs, evaluated back at the first ``k``
        points. More than ``n - k`` erasures is unrecoverable and raised, rather
        than returning a wrong message from an under-determined system.
        Not constant time.
        """
        if len(received) != self.n:
            raise ValueError(f"received must have {self.n} symbols, got {len(received)}")
        lost = set(erasures)
        survivors = [i for i in range(self.n) if i not in lost]
        if len(survivors) < self.k:
            raise ValueError(
                f"{len(lost)} erasures leave only {len(survivors)} symbols; need "
                f"at least k={self.k} to recover"
            )
        chosen = survivors[: self.k]
        points = [self._gen_powers[i] for i in chosen]
        values = [received[i] for i in chosen]
        coeffs = self._interpolate(points, values)
        return [self._eval(coeffs, self._gen_powers[i]) for i in range(self.k)]

    def _interpolate(self, points: list[int], values: list[int]) -> list[int]:
        """
        Lagrange interpolation over GF(2^8): the coefficients of the unique
        polynomial of degree < len(points) through the given pairs.
        """
        f = self._field
        result = [0] * len(points)
        for i, (xi, yi) in enumerate(zip(points, values, strict=True)):
            # Build the Lagrange basis polynomial L_i, scaled by y_i.
            basis = [1]
            denom = 1
            for j, xj in enumerate(points):
                if j == i:
                    continue
                # multiply basis by (x - xj) = (x + xj) in char 2
                basis = self._poly_mul_linear(basis, xj)
                denom = f.mul(denom, xi ^ xj)
            scale = f.mul(yi, f.inv(denom))
            for d, c in enumerate(basis):
                result[d] ^= f.mul(c, scale)
        return result

    def _poly_mul_linear(self, poly: list[int], root: int) -> list[int]:
        """Multiply ``poly`` by ``(x + root)`` over GF(2^8)."""
        f = self._field
        shifted = [0, *poly]  # times x
        scaled = [f.mul(c, root) for c in poly] + [0]
        return [a ^ b for a, b in zip(shifted, scaled, strict=True)]

    def _eval(self, coeffs: Sequence[int], x: int) -> int:
        """Evaluate a polynomial at ``x`` over GF(2^8) by Horner's rule."""
        f = self._field
        acc = 0
        for c in reversed(coeffs):
            acc = f.mul(acc, x) ^ c
        return acc
