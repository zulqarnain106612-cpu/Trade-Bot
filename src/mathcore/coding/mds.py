"""
MDS matrices and linear algebra over GF(2^8) and GF(2).

Owns two registry entries:

* ``mds-matrices`` -- a maximum-distance-separable matrix, the diffusion core of
  a block cipher. AES's MixColumns is one: every output byte depends on every
  input byte, and changing one input changes all outputs, which is what a
  cipher's avalanche rests on.
* ``linear-algebra-f2`` -- Gaussian elimination over GF(2), the workhorse behind
  rank computations, linear cryptanalysis, and the parity checks of a linear
  code.

The property that makes a matrix MDS is that *every* square submatrix is
invertible; equivalently the code it generates meets the Singleton bound. This
module checks that directly for small matrices and provides the GF(2) rank and
solver the check and the wider analytics need.

Public matrix arithmetic; constant-time does not apply.

References: FIPS 197 (MixColumns); MacWilliams & Sloane, *The Theory of
Error-Correcting Codes*, ch. 11; Daemen & Rijmen, *The Design of Rijndael*.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations

from ..fields.binary_field import AES_POLY, BinaryField

__all__ = [
    "AES_MIX_COLUMNS",
    "gf2_rank",
    "gf2_solve",
    "is_mds",
]

# The AES MixColumns matrix over GF(2^8), the canonical MDS example.
AES_MIX_COLUMNS = (
    (2, 3, 1, 1),
    (1, 2, 3, 1),
    (1, 1, 2, 3),
    (3, 1, 1, 2),
)


def _gf28() -> BinaryField:
    return BinaryField(AES_POLY)


def _submatrix_det(
    matrix: Sequence[Sequence[int]], rows: Sequence[int], cols: Sequence[int], field: BinaryField
) -> int:
    """Determinant of the chosen square submatrix over GF(2^8), by expansion."""
    size = len(rows)
    if size == 1:
        return matrix[rows[0]][cols[0]]
    total = 0
    first_row = rows[0]
    rest = rows[1:]
    for j, col in enumerate(cols):
        minor_cols = [c for k, c in enumerate(cols) if k != j]
        minor = _submatrix_det(matrix, rest, minor_cols, field)
        # Characteristic 2: every sign is +, so cofactors just XOR in.
        total ^= field.mul(matrix[first_row][col], minor)
    return total


def is_mds(matrix: Sequence[Sequence[int]]) -> bool:
    """
    Whether ``matrix`` is MDS over GF(2^8): every square submatrix invertible.

    A square submatrix over a field is invertible exactly when its determinant
    is non-zero, so this checks that every square submatrix of every order has
    non-zero determinant. That is the definition, not a proxy -- an MDS matrix
    is precisely one with no singular square submatrix, which is why a single
    zero minor disqualifies it. Exponential in the matrix size and so meant for
    the small diffusion matrices (4x4, 8x8) that actually occur. Not constant
    time.
    """
    rows = len(matrix)
    if rows == 0 or any(len(r) != rows for r in matrix):
        raise ValueError("is_mds requires a non-empty square matrix")
    field = _gf28()
    for order in range(1, rows + 1):
        for row_set in combinations(range(rows), order):
            for col_set in combinations(range(rows), order):
                if _submatrix_det(matrix, row_set, col_set, field) == 0:
                    return False
    return True


def gf2_rank(matrix: Sequence[Sequence[int]]) -> int:
    """
    The rank of a 0/1 matrix over GF(2), by Gaussian elimination.

    Row reduction with XOR as addition. The rank is the number of pivots, which
    over GF(2) is exactly the dimension of the row space -- the quantity linear
    cryptanalysis and code parity checks turn on. Non-0/1 entries are rejected,
    since a value other than a bit is a caller error over GF(2). Not constant
    time.
    """
    grid = [list(row) for row in matrix]
    if not grid:
        return 0
    width = len(grid[0])
    if any(len(r) != width for r in grid):
        raise ValueError("matrix must be rectangular")
    if any(v not in (0, 1) for row in grid for v in row):
        raise ValueError("GF(2) entries must be 0 or 1")

    rank = 0
    pivot_row = 0
    for col in range(width):
        pivot = next((r for r in range(pivot_row, len(grid)) if grid[r][col]), None)
        if pivot is None:
            continue
        grid[pivot_row], grid[pivot] = grid[pivot], grid[pivot_row]
        for r in range(len(grid)):
            if r != pivot_row and grid[r][col]:
                grid[r] = [a ^ b for a, b in zip(grid[r], grid[pivot_row], strict=True)]
        pivot_row += 1
        rank += 1
        if pivot_row == len(grid):
            break
    return rank


def gf2_solve(matrix: Sequence[Sequence[int]], rhs: Sequence[int]) -> list[int] | None:
    """
    Solve ``matrix @ x == rhs`` over GF(2), or return ``None`` if inconsistent.

    Returns one solution when the system is solvable (any solution, when the
    system is under-determined), and ``None`` when the augmented rank exceeds
    the coefficient rank -- an inconsistent system, which has no solution rather
    than a wrong one. Not constant time.
    """
    rows = [list(row) for row in matrix]
    if not rows:
        raise ValueError("system must have at least one equation")
    width = len(rows[0])
    if any(len(r) != width for r in rows):
        raise ValueError("matrix must be rectangular")
    if len(rhs) != len(rows):
        raise ValueError("rhs length must match the number of equations")
    if any(v not in (0, 1) for row in rows for v in row) or any(v not in (0, 1) for v in rhs):
        raise ValueError("GF(2) entries must be 0 or 1")

    augmented = [row + [b] for row, b in zip(rows, rhs, strict=True)]
    pivot_row = 0
    pivot_cols: list[int] = []
    for col in range(width):
        pivot = next((r for r in range(pivot_row, len(augmented)) if augmented[r][col]), None)
        if pivot is None:
            continue
        augmented[pivot_row], augmented[pivot] = augmented[pivot], augmented[pivot_row]
        for r in range(len(augmented)):
            if r != pivot_row and augmented[r][col]:
                augmented[r] = [
                    a ^ b for a, b in zip(augmented[r], augmented[pivot_row], strict=True)
                ]
        pivot_cols.append(col)
        pivot_row += 1
        if pivot_row == len(augmented):
            break

    for r in range(pivot_row, len(augmented)):
        if augmented[r][width] and not any(augmented[r][:width]):
            return None  # 0 == 1: inconsistent

    solution = [0] * width
    for i, col in enumerate(pivot_cols):
        solution[col] = augmented[i][width]
    return solution
