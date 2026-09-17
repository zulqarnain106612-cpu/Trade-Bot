"""
Number-theoretic primitives for :mod:`mathcore`.

``residues`` owns the Legendre/Jacobi symbols and Tonelli-Shanks square roots;
``primality`` owns Baillie-PSW and the Lucas sequences under it; ``crt`` owns
the Chinese Remainder Theorem and the fault check RSA-CRT requires;
``factorization``, ``continued_fractions`` and ``dlp_bounds`` own the RSA/DLP
analytics -- factoring, the Wiener attack, and the security-level bounds. The
registry, not this file list, is the record of what exists.
"""

from .continued_fractions import (
    continued_fraction,
    convergents,
    wiener_attack,
)
from .crt import (
    crt,
    crt_pair,
    detect_crt_fault,
    rsa_crt_recombine,
)
from .dlp_bounds import (
    generic_dlp_security_bits,
    nfs_cost_bits,
    pohlig_hellman_security_bits,
)
from .factorization import (
    factorize,
    fermat_factor,
    pollard_rho,
    trial_division,
)
from .primality import (
    SMALL_PRIMES,
    is_probable_prime,
    lucas_sequence,
    selfridge_parameters,
    strong_lucas_probable_prime,
    strong_probable_prime,
)
from .residues import (
    is_quadratic_residue,
    jacobi_symbol,
    legendre_symbol,
    sqrt_mod_prime,
)

__all__ = [
    "SMALL_PRIMES",
    "continued_fraction",
    "convergents",
    "crt",
    "crt_pair",
    "detect_crt_fault",
    "factorize",
    "fermat_factor",
    "generic_dlp_security_bits",
    "is_probable_prime",
    "is_quadratic_residue",
    "jacobi_symbol",
    "legendre_symbol",
    "lucas_sequence",
    "nfs_cost_bits",
    "pohlig_hellman_security_bits",
    "pollard_rho",
    "rsa_crt_recombine",
    "selfridge_parameters",
    "sqrt_mod_prime",
    "strong_lucas_probable_prime",
    "strong_probable_prime",
    "trial_division",
    "wiener_attack",
]
