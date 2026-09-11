"""
Tests for :mod:`src.mathcore.coding`.

Reed-Solomon is exercised as the property that defines it -- any n-k erasures
recover -- across small codes fast enough for CI. MDS is checked against the
AES MixColumns matrix (a known MDS matrix) and a known non-MDS one, and the
GF(2) linear algebra against hand-computable ranks and solutions.
"""

from __future__ import annotations

import random

import pytest

from src.mathcore.coding.mds import (
    AES_MIX_COLUMNS,
    gf2_rank,
    gf2_solve,
    is_mds,
)
from src.mathcore.coding.reed_solomon import ReedSolomon

# ---- Reed-Solomon ----------------------------------------------------------


@pytest.mark.parametrize(("n", "k"), [(10, 6), (7, 3), (16, 8), (20, 12), (5, 4)])
def test_any_n_minus_k_erasures_recover(n: int, k: int) -> None:
    """The defining MDS property: n-k parity symbols correct n-k erasures."""
    rng = random.Random(f"rs-{n}-{k}")
    rs = ReedSolomon(n, k)
    for _ in range(15):
        message = [rng.randrange(256) for _ in range(k)]
        code = rs.encode(message)
        erasures = rng.sample(range(n), n - k)
        received = [c if i not in erasures else 0 for i, c in enumerate(code)]
        assert rs.decode(received, erasures) == message


def test_the_code_is_systematic() -> None:
    rs = ReedSolomon(10, 6)
    message = [1, 2, 3, 4, 5, 6]
    assert rs.encode(message)[:6] == message


def test_no_erasures_recovers_trivially() -> None:
    rs = ReedSolomon(10, 6)
    message = [7, 8, 9, 10, 11, 12]
    code = rs.encode(message)
    assert rs.decode(code, []) == message


def test_fewer_erasures_than_parity_still_recover() -> None:
    rs = ReedSolomon(10, 6)
    message = [1, 2, 3, 4, 5, 6]
    code = rs.encode(message)
    received = [c if i != 2 else 0 for i, c in enumerate(code)]
    assert rs.decode(received, [2]) == message


def test_too_many_erasures_is_refused() -> None:
    rs = ReedSolomon(10, 6)
    code = rs.encode([1, 2, 3, 4, 5, 6])
    with pytest.raises(ValueError, match="need"):
        rs.decode(code, list(range(5)))  # 5 erasures, only 4 parity


@pytest.mark.parametrize(("n", "k"), [(5, 5), (5, 0), (4, 6), (256, 200)])
def test_degenerate_dimensions_are_refused(n: int, k: int) -> None:
    with pytest.raises(ValueError):
        ReedSolomon(n, k)


def test_a_non_byte_symbol_is_refused() -> None:
    rs = ReedSolomon(10, 6)
    with pytest.raises(ValueError, match="byte"):
        rs.encode([256, 0, 0, 0, 0, 0])


def test_wrong_message_length_is_refused() -> None:
    rs = ReedSolomon(10, 6)
    with pytest.raises(ValueError, match="6 symbols"):
        rs.encode([1, 2, 3])


# ---- MDS matrices ----------------------------------------------------------


def test_aes_mix_columns_is_mds() -> None:
    assert is_mds(AES_MIX_COLUMNS)


def test_a_matrix_with_a_zero_entry_is_not_mds() -> None:
    """A zero entry is a singular 1x1 submatrix, so the identity is not MDS."""
    assert not is_mds([[1, 0], [0, 1]])


def test_a_singular_matrix_is_not_mds() -> None:
    assert not is_mds([[1, 1], [1, 1]])


def test_a_2x2_all_distinct_nonzero_matrix_can_be_mds() -> None:
    """Over GF(2^8), [[1,1],[1,2]] has all 1x1 minors nonzero and det = 1."""
    assert is_mds([[1, 1], [1, 2]])


def test_is_mds_requires_a_square_matrix() -> None:
    with pytest.raises(ValueError, match="square"):
        is_mds([[1, 2, 3], [4, 5, 6]])
    with pytest.raises(ValueError, match="square"):
        is_mds([])


# ---- GF(2) linear algebra --------------------------------------------------


def test_rank_of_the_identity_is_full() -> None:
    assert gf2_rank([[1, 0, 0], [0, 1, 0], [0, 0, 1]]) == 3


def test_rank_detects_a_dependent_row() -> None:
    # Third row is the XOR of the first two.
    assert gf2_rank([[1, 1, 0], [0, 1, 1], [1, 0, 1]]) == 2


def test_rank_of_the_zero_matrix_is_zero() -> None:
    assert gf2_rank([[0, 0], [0, 0]]) == 0
    assert gf2_rank([]) == 0


def test_solve_finds_a_consistent_solution() -> None:
    matrix = [[1, 1, 0], [0, 1, 1]]
    rhs = [1, 1]
    x = gf2_solve(matrix, rhs)
    assert x is not None
    # verify matrix @ x == rhs over GF(2)
    for row, b in zip(matrix, rhs, strict=True):
        assert sum(a & xi for a, xi in zip(row, x, strict=True)) % 2 == b


def test_solve_returns_none_for_an_inconsistent_system() -> None:
    assert gf2_solve([[1, 1], [1, 1]], [0, 1]) is None


def test_solve_handles_the_homogeneous_system() -> None:
    assert gf2_solve([[1, 1, 0], [0, 1, 1], [1, 0, 1]], [0, 0, 0]) == [0, 0, 0]


def test_linear_algebra_rejects_non_bit_entries() -> None:
    with pytest.raises(ValueError, match="0 or 1"):
        gf2_rank([[2, 0], [0, 1]])
    with pytest.raises(ValueError, match="0 or 1"):
        gf2_solve([[1, 0], [0, 1]], [0, 2])


def test_solve_validates_shapes() -> None:
    with pytest.raises(ValueError, match="rhs length"):
        gf2_solve([[1, 0], [0, 1]], [1])
    with pytest.raises(ValueError, match="at least one equation"):
        gf2_solve([], [])


def test_decode_rejects_a_wrong_length_codeword() -> None:
    rs = ReedSolomon(10, 6)
    with pytest.raises(ValueError, match="10 symbols"):
        rs.decode([0, 0, 0], [])


def test_rank_rejects_a_ragged_matrix() -> None:
    with pytest.raises(ValueError, match="rectangular"):
        gf2_rank([[1, 0], [1]])


def test_solve_rejects_a_ragged_matrix() -> None:
    with pytest.raises(ValueError, match="rectangular"):
        gf2_solve([[1, 0], [1]], [0, 1])
