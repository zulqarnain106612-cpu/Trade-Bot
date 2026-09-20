"""
SECR-003 — keys are environment-separated and rotatable.

Two failures, one module.

The first is the shared key. A developer debugging against production
credentials, a paper deployment signing with the live key, a CI job that
happens to have the real secret in its environment: in each case nothing
looks wrong until the wrong account trades. Separation cannot be a convention
about which value goes in which `.env` file, because the whole failure mode is
that the same value ends up in two of them. Here the environment is an input
to the derivation, so two environments *cannot* produce the same key material
even when handed the identical master seed.

The second is rotation. A key that cannot be rotated will not be rotated, and
the reasons to rotate are not all emergencies: scheduled rotation, a
compromise, someone leaving, a server being rebuilt. Those need different
speeds, and the difference that matters is whether the old key keeps working
during the handover. So rotation is a generation counter with an explicit
reason, and the reason decides whether the previous generation stays valid.

What this module does *not* do is store anything. It derives, and it says what
is allowed; where the master seed lives is the deployment's problem and is
documented in `docs/security/KEY_MANAGEMENT.md`.

post_quantum posture (LAW12):
  No migration applies. The derivation here is HMAC-SHA-512 and HMAC-SHA-256
  -- symmetric constructions, and Grover's algorithm costs a square root of
  the search space, which halves an effective 256-bit key to a still
  unreachable 128 bits. There is no public-key operation in this path and no
  shared secret being established, so ML-KEM and ML-DSA have nothing to
  replace.

  What *would* change it: a future generation deriving an asymmetric key pair
  here, or the seed being transported rather than held. Either turns this into
  a module with a key-exchange or signature primitive in it, and the posture
  would have to be restated.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum
from typing import Final

from src.security.credential_vault import CredentialVault, DerivedKey


class Environment(Enum):
    """Every environment that may ever hold key material."""

    DEVELOPMENT = "development"
    TEST = "test"
    PAPER = "paper"
    PRODUCTION = "production"


class RotationReason(Enum):
    """
    Why a key is being rotated, which decides how the handover behaves.

    The distinction is not bookkeeping. A scheduled rotation that instantly
    invalidates the old key takes the bot offline mid-cycle; a compromise
    rotation that leaves the old key working has not contained anything.
    """

    SCHEDULED = "scheduled"
    COMPROMISE = "compromise"
    DEPARTURE = "departure"
    SERVER_REBUILD = "server_rebuild"


# Reasons after which the previous generation must stop working immediately.
# Compromise is obvious. Departure is here too: the person who left knows the
# old key, so an overlap window is a window in which they can still act.
_IMMEDIATE_REVOCATION: Final[frozenset[RotationReason]] = frozenset(
    {RotationReason.COMPROMISE, RotationReason.DEPARTURE}
)

# Environments whose keys may sign an order that spends real money.
LIVE_ENVIRONMENTS: Final[frozenset[Environment]] = frozenset({Environment.PRODUCTION})

_DOMAIN: Final[bytes] = b"trade-bot/key-lifecycle/v1"


@dataclass(frozen=True)
class KeyIdentity:
    """Everything that distinguishes one key from another."""

    environment: Environment
    exchange: str
    generation: int

    def label(self) -> str:
        """Stable, loggable name. Contains no key material."""
        return f"{self.environment.value}:{self.exchange}:g{self.generation}"


class KeySeparationError(RuntimeError):
    """A key is being used somewhere it does not belong."""


def derive_key(seed: bytes, identity: KeyIdentity) -> str:
    """
    Derive the API key for *identity* from the master *seed*.

    The environment, exchange and generation are all bound into the
    derivation, so no two identities can collide and no amount of copying the
    seed between environments makes two environments share a key.
    """
    if identity.generation < 0:
        raise ValueError("generation must not be negative")
    if not identity.exchange:
        raise ValueError("exchange must be named")

    vault = CredentialVault(seed)
    base: DerivedKey = vault.derive(identity.exchange, key_index=identity.generation)

    # HMAC rather than a hash of a concatenation: the parts are
    # attacker-influenced in principle (an exchange name comes from config),
    # and h(a || b) is ambiguous about where a ends.
    tag = hmac.new(
        key=base.private_key_bytes,
        msg=_DOMAIN
        + b"|"
        + identity.environment.value.encode()
        + b"|"
        + identity.exchange.lower().encode()
        + b"|"
        + str(identity.generation).encode(),
        digestmod=hashlib.sha256,
    )
    return tag.hexdigest()


def assert_environments_are_separated(seed: bytes, exchange: str) -> None:
    """
    Raise unless every environment derives distinct material from one seed.

    A property test would catch a regression here; this is the same check
    available at runtime, so a deployment can assert it at startup rather
    than trusting that the test suite ran.
    """
    keys = {env: derive_key(seed, KeyIdentity(env, exchange, 0)) for env in Environment}
    if len(set(keys.values())) != len(keys):
        raise KeySeparationError("two environments derive the same key material for " + exchange)


def assert_usable_in(identity: KeyIdentity, environment: Environment) -> None:
    """
    Raise unless *identity* belongs to *environment*.

    Called at the point of use, not at construction: a key object handed
    across a boundary is exactly how a production credential reaches a paper
    run, and the boundary is where the mismatch is visible.
    """
    if identity.environment is not environment:
        raise KeySeparationError(f"key {identity.label()} may not be used in {environment.value}")


def rotate(identity: KeyIdentity, reason: RotationReason) -> KeyIdentity:
    """Return the next generation of *identity*."""
    if not isinstance(reason, RotationReason):
        # A string reason would make `previous_generation_still_valid` a
        # silent "no revocation", which is the wrong default to reach by
        # accident.
        raise TypeError("reason must be a RotationReason")
    return KeyIdentity(identity.environment, identity.exchange, identity.generation + 1)


def previous_generation_still_valid(reason: RotationReason) -> bool:
    """
    Whether the superseded key keeps working during the handover.

    False for compromise and departure -- the two cases where somebody other
    than the operator knows the old key.
    """
    return reason not in _IMMEDIATE_REVOCATION
