"""
Lattice algorithms for :mod:`mathcore`.

``lll`` owns ``lattice-reduction-lll``; ``hnp`` owns ``hidden-number-problem``
and turns LLL into ECDSA key recovery from biased nonces. Read ``hnp``'s module
docstring: it is an attack, run by the defender on their own signatures.
"""

from .hnp import Signature, recover_private_key, recovery_limits
from .lll import (
    DEFAULT_DELTA,
    gram_schmidt,
    is_lll_reduced,
    lll_reduce,
    lll_reduce_with_transform,
)

__all__ = [
    "DEFAULT_DELTA",
    "Signature",
    "gram_schmidt",
    "is_lll_reduced",
    "lll_reduce",
    "lll_reduce_with_transform",
    "recover_private_key",
    "recovery_limits",
]
