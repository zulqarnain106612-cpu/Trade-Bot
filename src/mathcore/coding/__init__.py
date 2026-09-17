"""
Coding theory for :mod:`mathcore`.

``reed_solomon`` owns ``reed-solomon``; ``mds`` owns ``mds-matrices`` and
``linear-algebra-f2``. Together: erasure coding, the diffusion matrices of a
block cipher, and Gaussian elimination over GF(2).
"""

from .mds import AES_MIX_COLUMNS, gf2_rank, gf2_solve, is_mds
from .reed_solomon import ReedSolomon

__all__ = [
    "AES_MIX_COLUMNS",
    "ReedSolomon",
    "gf2_rank",
    "gf2_solve",
    "is_mds",
]
