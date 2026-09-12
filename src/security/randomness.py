"""
SECR-007 — unpredictable values come from the OS CSPRNG.

`random` is a Mersenne Twister. It is fast, reproducible, excellent for
simulation, and completely broken for anything an adversary may guess: 624
consecutive outputs recover the entire internal state, after which every past
and future value is known. That matters here because this bot generates
exactly the kinds of values that property applies to -- request nonces, idempotency
keys, session and client identifiers.

The failure is quiet. `random.randint` produces a perfectly plausible-looking
number, nothing errors, and the weakness is only visible to somebody who
collects enough of them. So the defence is structural rather than reviewed:
every unpredictable value in this codebase comes from a named function here,
each of which is a thin wrapper over `secrets`, and a test walks the source
tree asserting that nothing else reaches for `random` in a security context.

Thin wrappers are the point. They give the static check something to look for,
and they put the length policy in one place rather than at each call site,
where "16 hex characters" quietly becomes 8.

post_quantum posture (LAW12):
  No migration applies. This module has no cryptographic primitive to
  replace -- it reads the operating system's CSPRNG and formats the result.
  A quantum adversary does not get a better attack on `os.urandom`; the
  relevant bound is Grover, which halves effective entropy, and the 128-bit
  floor here leaves 64 bits of quantum search on the smallest token. ML-KEM
  and ML-DSA are about key establishment and signatures, neither of which
  happens here.

  What *would* change it: raising a token's security target above 128 bits
  post-Grover, which means raising MIN_TOKEN_BITS to 256 rather than swapping
  any algorithm.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Final

# 128 bits. The floor for a value that must not collide or be guessed: below
# this, birthday collisions on a busy day stop being theoretical.
MIN_TOKEN_BITS: Final[int] = 128
_MIN_TOKEN_BYTES: Final[int] = MIN_TOKEN_BITS // 8

NONCE_BYTES: Final[int] = 16
IDEMPOTENCY_KEY_BYTES: Final[int] = 16
SESSION_TOKEN_BYTES: Final[int] = 32


def _checked(nbytes: int) -> int:
    if nbytes < _MIN_TOKEN_BYTES:
        raise ValueError(f"refusing to generate fewer than {MIN_TOKEN_BITS} bits of randomness")
    return nbytes


def random_bytes(nbytes: int = _MIN_TOKEN_BYTES) -> bytes:
    """Raw CSPRNG bytes."""
    return secrets.token_bytes(_checked(nbytes))


def random_hex(nbytes: int = _MIN_TOKEN_BYTES) -> str:
    """Hex token. Twice *nbytes* characters."""
    return secrets.token_hex(_checked(nbytes))


def random_urlsafe(nbytes: int = _MIN_TOKEN_BYTES) -> str:
    """URL-safe token, for anything that travels in a header or a query."""
    return secrets.token_urlsafe(_checked(nbytes))


def new_nonce() -> str:
    """A request nonce. Unpredictable and single-use by construction."""
    return random_hex(NONCE_BYTES)


def new_idempotency_key() -> str:
    """
    An idempotency key for an order.

    Guessable keys are worse than merely predictable: a caller who can guess
    one can collide with somebody else's in-flight order and get that order's
    result back instead of placing their own.
    """
    return random_hex(IDEMPOTENCY_KEY_BYTES)


def new_session_token() -> str:
    """A bearer-style token. Longer, because it is a standing credential."""
    return random_urlsafe(SESSION_TOKEN_BYTES)


def new_identifier() -> str:
    """
    A public identifier, such as a client order id.

    `uuid4` rather than a counter: a sequential id tells every holder of one
    how many exist and lets them address the neighbours.
    """
    return str(uuid.uuid4())


def below_threshold(numerator: int, denominator: int) -> bool:
    """
    A fair coin-flip style decision, CSPRNG-backed.

    Present so that sampling decisions which gate a *security* behaviour --
    which request to audit in depth, which order to double-check -- do not
    have to reach for `random.random()` and thereby become predictable to
    whoever is being sampled.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return secrets.randbelow(denominator) < numerator
