"""
SECR-009 — sensitive data at rest is encrypted, and the restore path works.

Encryption at rest is the control most often declared and least often
exercised. The write path gets used every day; the read path gets used on the
worst day of the year, when a database is gone and somebody is reaching for a
backup under time pressure. A backup that cannot be decrypted is not a backup,
and the only way to know is to decrypt one on an ordinary day.

So this module is deliberately small and the tests around it are mostly about
the read path: wrong key, truncated file, flipped byte, wrong version, right
key but a file written by a previous format. Every one of those is a thing
that has happened to somebody mid-restore.

AES-256-GCM via `cryptography`, not a construction of our own (SECR-008): the
authentication tag is what turns "the ciphertext was modified" from silent
garbage into an exception, and getting an AEAD right by hand is not a thing to
attempt. The key is supplied by the caller -- where it lives is deployment
policy, documented in `docs/security/KEY_MANAGEMENT.md`, and it must not be
the same key as anything else this bot holds.

post_quantum posture (LAW12):
  No migration applies to the primitive, but the exposure class is the worst
  one. AES-256-GCM is symmetric: Grover halves the effective key to 128 bits,
  which is out of reach, so the cipher itself needs no replacement -- and
  AES-256 rather than AES-128 is chosen with exactly that halving in mind.

  The real quantum question here is harvest-now-decrypt-later, and this module
  is on the right side of it *provided the key never travels*. A backup
  encrypted today and stolen today stays unreadable; a key wrapped for
  transport under ECDH or RSA does not. So the posture is a constraint, not a
  clean bill: keys are delivered out of band and never key-wrapped with a
  classical public-key scheme.

  What *would* change it: introducing envelope encryption with an asymmetric
  key-wrapping step. That step needs hybrid ML-KEM from day one, because the
  data it protects is long-lived by definition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Envelope: magic || version || nonce || ciphertext-with-tag. The magic makes
# an encrypted blob identifiable in a directory listing during a restore, and
# the version makes a future format change a clear error rather than a
# confusing authentication failure.
MAGIC: Final[bytes] = b"TBENC"
VERSION: Final[int] = 1
NONCE_BYTES: Final[int] = 12  # GCM's standard nonce size
KEY_BYTES: Final[int] = 32  # AES-256
_HEADER_LEN: Final[int] = len(MAGIC) + 1 + NONCE_BYTES


class DecryptionError(RuntimeError):
    """
    A ciphertext that could not be recovered.

    One exception type for every reason -- wrong key, truncated file, flipped
    byte, wrong version. A caller mid-restore needs to know the file is
    unusable; which of the four it is comes from the message, and none of the
    four should be separately catchable, because branching on them is how a
    "try the other key" loop turns into an oracle.
    """


@dataclass(frozen=True)
class EncryptedBlob:
    """Parsed envelope, for tests and for a restore tool to inspect."""

    version: int
    nonce: bytes
    ciphertext: bytes


def generate_key() -> bytes:
    """A fresh AES-256 key from the OS CSPRNG."""
    return AESGCM.generate_key(bit_length=256)


def _validated_key(key: bytes) -> bytes:
    if not isinstance(key, bytes | bytearray) or len(key) != KEY_BYTES:
        raise ValueError(f"key must be exactly {KEY_BYTES} bytes")
    return bytes(key)


def encrypt(plaintext: bytes, key: bytes, associated_data: bytes | None = None) -> bytes:
    """
    Encrypt *plaintext*, returning the full envelope.

    `associated_data` is authenticated but not encrypted -- use it for the
    thing that says what this blob *is* (a backup name, a table, a date), so
    that a blob cannot be silently swapped for another one encrypted under
    the same key.
    """
    aes = AESGCM(_validated_key(key))
    nonce = os.urandom(NONCE_BYTES)
    ct = aes.encrypt(nonce, plaintext, associated_data)
    return MAGIC + bytes([VERSION]) + nonce + ct


def parse(blob: bytes) -> EncryptedBlob:
    """Split an envelope without decrypting it."""
    if len(blob) <= _HEADER_LEN:
        raise DecryptionError("blob is truncated")
    if not blob.startswith(MAGIC):
        raise DecryptionError("blob is not in this format")
    version = blob[len(MAGIC)]
    nonce = blob[len(MAGIC) + 1 : _HEADER_LEN]
    return EncryptedBlob(version=version, nonce=nonce, ciphertext=blob[_HEADER_LEN:])


def decrypt(blob: bytes, key: bytes, associated_data: bytes | None = None) -> bytes:
    """
    Recover the plaintext, or raise `DecryptionError`.

    This is the path that matters. Every failure mode below has been somebody's
    outage: the wrong key from an old vault entry, a file truncated by a
    partial download, a byte flipped in transit, a blob from a format that
    predates the current code.
    """
    parsed = parse(blob)
    if parsed.version != VERSION:
        raise DecryptionError(
            f"blob version {parsed.version} is not supported by this build (expected {VERSION})"
        )
    try:
        aes = AESGCM(_validated_key(key))
        return aes.decrypt(parsed.nonce, parsed.ciphertext, associated_data)
    except InvalidTag as exc:
        # Covers wrong key, modified ciphertext and mismatched associated
        # data alike -- deliberately indistinguishable, as GCM intends.
        raise DecryptionError("blob failed authentication: wrong key or modified data") from exc
    except ValueError as exc:
        raise DecryptionError(str(exc)) from exc


def round_trip_check(key: bytes) -> bool:
    """
    Prove the read path works, right now, with this key.

    Intended to be called at startup and by the restore drill: an encryption
    control that is never decrypted is a claim, not a control.
    """
    probe = b"restore-drill"
    return decrypt(encrypt(probe, key, b"drill"), key, b"drill") == probe
