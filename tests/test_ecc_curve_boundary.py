"""
The on-curve boundary for :mod:`src.ecc`.

The roadmap's phase-2 exit gate asks that every point entering ``src/ecc/`` be
on-curve-checked, and that a test assert an off-curve point is rejected *at the
boundary* rather than deeper in. This is that test.

``extract_ecdsa_signatures`` scans raw transaction bytes for DER signatures and
takes the 33 bytes after each one as a compressed public key. A ``0x02``/``0x03``
prefix and the right length are not a public key: roughly half of all 32-byte
strings have no point on secp256k1, so an unchecked match is close to a coin
flip. Everything downstream -- nonce-reuse clustering, key recovery, the
weakness score -- treats what comes out of that function as a real key, so the
check has to happen there and nowhere later.
"""

from __future__ import annotations

import pytest

from src.ecc.ecdsa_scan import extract_ecdsa_signatures
from src.mathcore.curves.secp256k1 import GENERATOR, decompress, parse_point


def _der(r: int, s: int) -> bytes:
    """A minimal DER-encoded ECDSA signature."""

    def integer(value: int) -> bytes:
        body = value.to_bytes(32, "big")
        if body[0] & 0x80:
            body = b"\x00" + body
        return b"\x02" + bytes([len(body)]) + body

    payload = integer(r) + integer(s)
    return b"\x30" + bytes([len(payload)]) + payload


def _raw_transaction(pubkey_x: int, prefix: int = 0x02) -> str:
    """
    Synthetic transaction bytes: a DER signature, a sighash byte, then a
    33-byte compressed key with the given x coordinate.
    """
    body = (
        b"\x00" * 4
        + _der(0x11, 0x22)
        + b"\x01"
        + bytes([prefix])
        + pubkey_x.to_bytes(32, "big")
        + b"\x00" * 8
    )
    return body.hex()


@pytest.fixture(scope="module")
def off_curve_x() -> int:
    """The smallest x with no point on secp256k1."""
    x = next(x for x in range(1, 200) if decompress(x, y_is_odd=False) is None)
    assert parse_point(b"\x02" + x.to_bytes(32, "big")) is None
    return x


def test_an_on_curve_public_key_is_extracted() -> None:
    """The control: without this, rejecting everything would pass the test."""
    found = extract_ecdsa_signatures(_raw_transaction(GENERATOR.x))
    assert len(found) == 1
    r, s, pubkey, _txid = found[0]
    assert (r, s) == (0x11, 0x22)
    assert parse_point(pubkey) is not None


def test_an_off_curve_public_key_is_rejected_at_the_boundary(off_curve_x: int) -> None:
    """
    Byte-for-byte the same transaction but for the x coordinate, which is not
    on the curve. Nothing is extracted -- the rejection happens here, not in a
    later stage that would have to guess it was handed garbage.
    """
    assert extract_ecdsa_signatures(_raw_transaction(off_curve_x)) == []


def test_both_parity_prefixes_are_checked(off_curve_x: int) -> None:
    """An off-curve x has no point of either parity; neither prefix rescues it."""
    assert extract_ecdsa_signatures(_raw_transaction(off_curve_x, prefix=0x03)) == []
    assert len(extract_ecdsa_signatures(_raw_transaction(GENERATOR.x, prefix=0x03))) == 1


def test_every_extracted_public_key_is_on_the_curve() -> None:
    """
    The invariant stated as a property rather than a case: whatever the input,
    nothing leaves this function that is not a point.
    """
    inputs = [_raw_transaction(x) for x in (GENERATOR.x, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    inputs += ["", "00", "ff" * 64, "30" * 80]
    for raw in inputs:
        for _r, _s, pubkey, _txid in extract_ecdsa_signatures(raw):
            assert parse_point(pubkey) is not None, pubkey.hex()
