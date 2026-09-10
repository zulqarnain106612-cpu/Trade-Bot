"""
mathcore — the mathematical substrate this project is allowed to rely on.

Every object in here is registered in ``config/math_registry.json`` with an
explicit applicability verdict. That registry, not this package's file list, is
the source of truth: a module may only claim to own a registry entry, and an
entry marked ``folklore`` may never become a live trading signal without
passing the validation gate its own entry names.

The package is deliberately layered so that the pure, deterministic parts have
no dependency on the parts that touch a network or a key:

    fields/        finite field arithmetic and the NTT
    curves/        elliptic curve parameters and verification-only point ops
    numbertheory/  primality, residues, CRT, continued fractions, DLP bounds
    harmonic/      Walsh-Hadamard, spectral side-channel analysis, lattice Gaussians
    lattice/       LLL/BKZ reduction and the hidden number problem
    coding/        Reed-Solomon and MDS matrices
    probability/   birthday bounds, confirmation depth, entropy, committee bounds
    commitments/   Merkle trees
    derivation/    BIP-32, deterministic nonces, threshold signing
    constants/     nothing-up-my-sleeve constant re-derivation
    folklore/      the gate that keeps numerology out of the strategy layer

Only ``registry`` is implemented at this layer today; the rest are specified in
``docs/MATH_ARCHITECTURE.md`` with their contracts fixed, and are built in the
phase order given in ``docs/MATH_ROADMAP.md``.
"""

from __future__ import annotations

from src.mathcore.registry import (
    MathRegistry,
    RegistryEntry,
    RegistryError,
    load_registry,
)

__all__ = ["MathRegistry", "RegistryEntry", "RegistryError", "load_registry"]
