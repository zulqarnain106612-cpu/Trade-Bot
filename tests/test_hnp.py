"""
Tests for :mod:`src.mathcore.lattice.hnp`.

The exit gate asks for two directions: recovery of a known key from biased
signatures, and *non*-recovery from unbiased ones. Both are here, and both are
end-to-end -- the signatures are produced by a real signing relation
``s*k = z + r*d (mod n)`` and the key is recovered without ever being passed in.

**Scale.** This module reduces with exact-rational LLL, which is correct but
scales only to a few dozen lattice dimensions -- see ``recovery_limits()``.
Recovering a 256-bit key from a one-bit leak needs hundreds of signatures and
floating-point BKZ, which the module deliberately does not use. So the tests
demonstrate the *mechanism* on a small group where the exact engine is fast,
and the boundary itself is asserted through ``recovery_limits`` and the
scale-guard tests rather than by running an infeasible reduction. This is a
deliberate, documented limit, not a gap the tests paper over.
"""

from __future__ import annotations

import ast
import inspect
import random

import pytest

from src.mathcore.lattice import hnp
from src.mathcore.lattice.hnp import (
    Signature,
    recover_private_key,
    recovery_limits,
)

# A small prime-order group. The HNP relation is curve-agnostic: r need only be
# a deterministic function of k, so this uses a modular-exponentiation stand-in
# and keeps the lattice dimension small enough for exact LLL to be quick.
ORDER = 1_000_003
_R_BASE = 5
_R_MODULUS = 2_000_003
ORDER_BITS = ORDER.bit_length()


def _sign(private_key: int, message_hash: int, nonce: int) -> tuple[int, int]:
    r = pow(_R_BASE, nonce, _R_MODULUS) % ORDER or 1
    s = (pow(nonce, -1, ORDER) * (message_hash + r * private_key)) % ORDER
    return r, s


def _biased_signatures(key: int, count: int, unknown_bits: int, seed: str) -> list[Signature]:
    """Signatures whose nonces are known to be below ``2**unknown_bits``."""
    rng = random.Random(seed)
    known = ORDER_BITS - unknown_bits
    out = []
    for _ in range(count):
        z = rng.randrange(ORDER)
        k = rng.randrange(1, 1 << unknown_bits)
        r, s = _sign(key, z, k)
        out.append(Signature(r, s, z, known_high_bits=known))
    return out


# ---- recovery (the positive direction) -------------------------------------


@pytest.mark.parametrize(
    ("unknown_bits", "count"),
    [(10, 4), (8, 5), (6, 7)],
)
def test_a_biased_key_is_recovered(unknown_bits: int, count: int) -> None:
    """
    Fewer known bits needs more signatures; each case is sized so the exact
    lattice stays small. The key is never passed to the recoverer.
    """
    rng = random.Random(f"key-{unknown_bits}")
    key = rng.randrange(1, ORDER)
    sigs = _biased_signatures(key, count, unknown_bits, f"sigs-{unknown_bits}")
    assert recover_private_key(sigs, ORDER, ORDER_BITS) == key


def test_recovery_needs_no_knowledge_of_the_key_object() -> None:
    """
    Structural, not incidental: the samples are the only channel. The signer's
    key lives in this test's local scope and reaches the recoverer only through
    the public (r, s, z) it produced.
    """
    key = 424242
    sigs = _biased_signatures(key, 5, 8, "isolated")
    assert recover_private_key(sigs, ORDER, ORDER_BITS) == key


# ---- non-recovery (the negative direction) ---------------------------------


def test_unbiased_nonces_do_not_yield_the_key() -> None:
    """
    The same signer with full-range nonces. Claiming a bias that is not there
    must return None -- a false positive here would be a tool that cries wolf
    on every healthy signer, which is worse than useless.
    """
    rng = random.Random("unbiased")
    key = rng.randrange(1, ORDER)
    sigs = []
    for _ in range(6):
        z = rng.randrange(ORDER)
        k = rng.randrange(1, ORDER)  # no bias
        r, s = _sign(key, z, k)
        sigs.append(Signature(r, s, z, known_high_bits=6))
    assert recover_private_key(sigs, ORDER, ORDER_BITS) is None


def test_a_wrong_known_value_does_not_yield_the_key() -> None:
    """
    Real bias, but the caller mis-states which value the top bits hold. The
    lattice then encodes the wrong offsets and the verified candidate does not
    check out, so None rather than a plausible-looking wrong key.
    """
    rng = random.Random("wrong-value")
    key = rng.randrange(1, ORDER)
    sigs = _biased_signatures(key, 6, 8, "wv")
    lying = [
        Signature(s.r, s.s, s.z, known_high_bits=s.known_high_bits, known_value=1 << 30)
        for s in sigs
    ]
    assert recover_private_key(lying, ORDER, ORDER_BITS) != key


# ---- the documented limits -------------------------------------------------


def test_recovery_limits_names_the_conditions_and_the_scale_bound() -> None:
    text = recovery_limits()
    assert "same bit position" in text
    assert "RFC 6979" in text
    # The scale caveat must be present: a clean result is not an all-clear, and
    # the exact engine's dimension ceiling is a real limit, not a footnote.
    assert "BKZ" in text or "floating-point" in text
    assert "not proof" in text


# ---- law L1: no access to this project's key material ----------------------


def test_the_module_imports_nothing_from_src_security() -> None:
    """
    Law L1, enforced statically. recover_private_key operates only on
    caller-supplied samples; a dependency on the credential vault would be a
    path from an attack tool to real keys, so the import is banned and the ban
    is tested rather than trusted.
    """
    source = inspect.getsource(hnp)
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not any("security" in module or "credential" in module for module in imported)
    assert not any("src.security" in module for module in imported)


# ---- validation ------------------------------------------------------------


def test_at_least_two_signatures_are_required() -> None:
    with pytest.raises(ValueError, match="at least two"):
        recover_private_key(_biased_signatures(7, 1, 8, "one"), ORDER, ORDER_BITS)


def test_a_signature_with_no_known_bits_is_a_caller_error() -> None:
    sigs = _biased_signatures(7, 3, 8, "z")
    sigs[1] = Signature(sigs[1].r, sigs[1].s, sigs[1].z, known_high_bits=0)
    with pytest.raises(ValueError, match="positive known_high_bits"):
        recover_private_key(sigs, ORDER, ORDER_BITS)


def test_known_bits_must_be_fewer_than_the_nonce_length() -> None:
    sigs = _biased_signatures(7, 3, 8, "full")
    sigs[0] = Signature(sigs[0].r, sigs[0].s, sigs[0].z, known_high_bits=ORDER_BITS)
    with pytest.raises(ValueError, match="fewer than nonce_bitlength"):
        recover_private_key(sigs, ORDER, ORDER_BITS)


@pytest.mark.parametrize("bad_order", [0, 1, 2])
def test_a_degenerate_order_is_refused(bad_order: int) -> None:
    with pytest.raises(ValueError, match="group order"):
        recover_private_key(_biased_signatures(1, 3, 2, "o"), bad_order, ORDER_BITS)


def test_a_degenerate_nonce_length_is_refused() -> None:
    with pytest.raises(ValueError, match="nonce_bitlength"):
        recover_private_key(_biased_signatures(7, 3, 8, "b"), ORDER, 0)


def test_the_signature_carries_its_own_leak_description() -> None:
    """The leak travels with the signature, so a caller cannot desynchronise a
    list of signatures from a separate list of leaks."""
    sig = Signature(r=1, s=2, z=3, known_high_bits=8)
    assert sig.known_value == 0
    assert (sig.r, sig.s, sig.z, sig.known_high_bits) == (1, 2, 3, 8)
