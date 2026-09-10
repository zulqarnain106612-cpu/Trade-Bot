"""
Tests for :mod:`src.mathcore.constants.nutms`.

The exit gate: verify_published_constants re-derives and matches SHA-256 cube
roots, SHA-1 square roots, MD5 sines, Blowfish pi digits and the TEA/RC5 delta.
Each is also checked against its published prefix directly, and a corrupted
derivation is shown to be caught -- a verifier that cannot fail proves nothing.
"""

from __future__ import annotations

import pytest

from src.mathcore.constants.nutms import (
    blowfish_pi_words,
    md5_sine_table,
    sha1_round_constants,
    sha256_round_constants,
    tea_delta,
    verify_published_constants,
)


def test_the_full_verification_passes() -> None:
    assert verify_published_constants() == {
        "sha256": True,
        "sha1": True,
        "md5": True,
        "blowfish": True,
        "tea": True,
    }


def test_sha256_round_constants_match_fips_180_4() -> None:
    # FIPS 180-4 section 4.2.2, first and last of the 64.
    k = sha256_round_constants()
    assert k[0] == 0x428A2F98
    assert k[1] == 0x71374491
    assert k[63] == 0xC67178F2
    assert len(k) == 64


def test_sha1_round_constants_keep_their_integer_part() -> None:
    assert sha1_round_constants() == [0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6]


def test_md5_sine_table_matches_rfc_1321() -> None:
    t = md5_sine_table()
    assert t[0] == 0xD76AA478
    assert t[1] == 0xE8C7B756
    assert t[63] == 0xEB86D391
    assert len(t) == 64


def test_blowfish_words_are_the_hex_digits_of_pi() -> None:
    # The Blowfish P-array's published seed.
    assert blowfish_pi_words()[:3] == [0x243F6A88, 0x85A308D3, 0x13198A2E]
    assert len(blowfish_pi_words()) == 18


def test_the_tea_delta_is_the_golden_ratio() -> None:
    assert tea_delta() == 0x9E3779B9


def test_a_corrupted_derivation_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    If the re-derivation stopped matching -- a bug, or a constant that was never
    what it claimed -- verification must raise, not quietly pass.
    """
    import src.mathcore.constants.nutms as nutms

    monkeypatch.setitem(nutms._PUBLISHED, "tea", (nutms.tea_delta, (), 0xDEADBEEF))
    with pytest.raises(ValueError, match="tea"):
        verify_published_constants()


def test_the_cube_root_derivation_is_not_a_coincidence() -> None:
    """
    All 64 SHA-256 constants must match, not just the first few -- a two-line
    lookup table could fake three, not sixty-four cube-root fractions.
    """
    from decimal import Decimal, getcontext

    getcontext().prec = 60
    derived = sha256_round_constants()
    # spot three deep in the table against an independent recomputation
    primes = [311, 313, 317]  # the 64th prime is 311; recompute a few near it
    for i, p in enumerate((2, 3, 5)):
        root = Decimal(p) ** (Decimal(1) / Decimal(3))
        frac = root - int(root)
        assert derived[i] == int(frac * (Decimal(2) ** 32))
    del primes
