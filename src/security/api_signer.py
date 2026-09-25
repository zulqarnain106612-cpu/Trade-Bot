"""
Ed25519 deterministic API request signing (Part II §9.1).

Replaces HMAC-SHA256 signing with Ed25519 (from cryptography package).
Ed25519 uses RFC 6979-equivalent deterministic nonce internally:
  - No random k → immune to entropy failures (Android 2013 / PS3 2010)
  - Deterministic: same message + key → same signature

Attack defended:
  - ECDSA k-reuse → full private key recovery (A1 from cross-layer table)
  - Kocher timing attack → Ed25519 complete-addition-law, constant time

post_quantum posture (LAW12):
  Ed25519 is a discrete-log signature and Shor's algorithm breaks it
  outright. The NIST replacement is ML-DSA (FIPS 204); ML-KEM is not
  relevant here, as nothing in this module establishes a shared secret.

  It is not urgent, and the reason is worth stating so nobody reprioritises
  it on the word "broken". These signatures authenticate API requests that
  carry a timestamp and are rejected outside a short window, so there is no
  harvest-now-decrypt-later exposure: a captured signature is worthless
  later, and forging one requires a cryptographically relevant quantum
  computer *while the request window is open*. That is the opposite of the
  situation for long-lived encrypted data.

  Migration is therefore a swap of the signing primitive, not a protocol
  change: sign_request/verify are the only two call sites, and the key is
  loaded as opaque bytes from the environment. The blocker is external --
  ML-DSA is not in `cryptography` as of the pinned version, and any
  counterparty verifying these signatures has to accept the new algorithm
  before we can emit it. Revisit when that lands.
"""

from __future__ import annotations

import base64
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final


class ApiSigner:
    """
    Signs Trade-Bot API requests with Ed25519.

    The private key is loaded from an env var or file — never hardcoded.
    """

    def __init__(self, private_key_b64: str | None = None) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if private_key_b64 is not None:
            raw = base64.b64decode(private_key_b64)
            self._key = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            self._key = Ed25519PrivateKey.generate()

    @classmethod
    def from_env(cls, env_var: str = "API_SIGNING_KEY_B64") -> ApiSigner:
        """Load key from environment variable (base64-encoded 32-byte seed)."""
        raw = os.environ.get(env_var, "")
        if raw:
            return cls(raw)
        return cls()  # ephemeral key for testing

    def sign_request(self, method: str, path: str, body: str, timestamp: int) -> str:
        """Return hex-encoded Ed25519 signature for the request."""
        payload = f"{timestamp}{method}{path}{body}".encode()
        sig = self._key.sign(payload)
        return sig.hex()

    def public_key_b64(self) -> str:
        """Return base64-encoded public key for verification."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        pub = self._key.public_key()
        raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def verify(self, method: str, path: str, body: str, timestamp: int, signature_hex: str) -> bool:
        """Verify a signature produced by sign_request."""
        from cryptography.exceptions import InvalidSignature

        try:
            payload = f"{timestamp}{method}{path}{body}".encode()
            sig = bytes.fromhex(signature_hex)
            pub = self._key.public_key()
            pub.verify(sig, payload)
            return True
        except (InvalidSignature, ValueError):
            return False


# ---------------------------------------------------------------------------
# SECR-005 -- freshness and replay
# ---------------------------------------------------------------------------

# A valid signature stays valid forever, which is the whole problem: an
# attacker who captures one signed "close all positions" can send it again
# tomorrow and it verifies. Signing proves authorship, never recency, so the
# timestamp is inside the signed payload (it already is, see sign_request) and
# these two checks are what make it mean anything.

MAX_TIMESTAMP_SKEW_S: Final[int] = 30

# Bounded, because the cache is filled by whoever is talking to us. Sized
# generously against the skew window: at any moment only signatures from the
# last MAX_TIMESTAMP_SKEW_S seconds can still verify, so a cache that holds
# more than one such window can never forget a signature that is still live.
_SEEN_CACHE_SIZE: Final[int] = 4096


class ReplayError(RuntimeError):
    """A signed request that is stale, post-dated, or already used."""


class ReplayWindow:
    """
    Remembers recently-accepted signatures and rejects a second use.

    One instance per verifying process. The signature is the replay key
    rather than a separate nonce: it is unique per (timestamp, method, path,
    body) already, so a client needs nothing new and there is no field an
    implementation can forget to send.
    """

    def __init__(self, now: Any = time.time) -> None:
        self._now = now
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check(self, signature_hex: str, timestamp: int) -> None:
        """Raise `ReplayError` unless this signature is fresh and unused."""
        now = float(self._now())
        skew = now - float(timestamp)
        if skew > MAX_TIMESTAMP_SKEW_S:
            raise ReplayError("request timestamp is too old")
        if -skew > MAX_TIMESTAMP_SKEW_S:
            # Post-dated requests are refused too: accepting one would let an
            # attacker mint a signature that stays replayable for as long as
            # they cared to post-date it.
            raise ReplayError("request timestamp is in the future")
        if signature_hex in self._seen:
            raise ReplayError("signature has already been used")
        self._seen[signature_hex] = now
        self._prune(now)

    def _prune(self, now: float) -> None:
        while self._seen and now - next(iter(self._seen.values())) > MAX_TIMESTAMP_SKEW_S:
            self._seen.popitem(last=False)
        while len(self._seen) > _SEEN_CACHE_SIZE:
            self._seen.popitem(last=False)


def verify_fresh_request(
    signer: ApiSigner,
    window: ReplayWindow,
    method: str,
    path: str,
    body: str,
    timestamp: int,
    signature_hex: str,
) -> None:
    """
    Full verification: authorship, then freshness, then single-use.

    Raises `ReplayError`. Signature first, so that an unauthenticated caller
    cannot fill the replay cache by sending garbage -- the cheap check is the
    one an attacker controls the volume of.
    """
    if not signer.verify(method, path, body, timestamp, signature_hex):
        raise ReplayError("signature does not verify")
    window.check(signature_hex, timestamp)


@dataclass(frozen=True)
class SignerAudit:
    """
    The result of auditing an :class:`ApiSigner`.

    ``clean`` is the single bit a caller should gate on: it is ``True`` only
    when every check passed and ``findings`` is empty. The individual booleans
    and the ``findings`` list are there so a non-clean result says *what* is
    wrong, because "signer failed audit" with no detail is not actionable.
    """

    algorithm: str
    deterministic: bool
    round_trip_ok: bool
    independent_verify_cofactorless: bool
    independent_verify_cofactored: bool
    tamper_rejected: bool
    findings: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return (
            self.deterministic
            and self.round_trip_ok
            and self.independent_verify_cofactorless
            and self.independent_verify_cofactored
            and self.tamper_rejected
            and not self.findings
        )


def audit_signer(signer: ApiSigner) -> SignerAudit:
    """
    Check that ``signer`` has the properties that make it safe to sign with.

    This is the defensive companion to :mod:`src.mathcore.lattice.hnp`: that
    module recovers a key from a signer with a biased nonce, and this one
    confirms the production signer has no nonce to bias. It checks, using only
    the signer's public interface -- never its private key:

    * **Determinism.** The same request signed twice must give an identical
      signature. Ed25519's nonce is derived from the key and message (RFC
      6979-equivalent), so a difference here would mean a randomised nonce and
      the entire biased-nonce attack surface reopening.
    * **Round trip.** The signer's own ``verify`` must accept its signature.
    * **Independent verification.** The signature must also verify under this
      project's from-scratch Ed25519 (:mod:`src.mathcore.curves.ed25519`),
      under *both* cofactor conventions -- proof the output is a real,
      canonical Ed25519 signature and not merely something the signer's own
      verifier is willing to accept.
    * **Tamper rejection.** A signature with one flipped byte must be refused.

    Returns a :class:`SignerAudit`; ``.clean`` is ``True`` only if every check
    passed. The audit signs a fixed synthetic request, so it has no side
    effects and reveals nothing secret.
    """
    from src.mathcore.curves import ed25519

    method, path, body, timestamp = "POST", "/v1/audit", '{"probe":true}', 1_700_000_000
    payload = f"{timestamp}{method}{path}{body}".encode()

    findings: list[str] = []

    first = signer.sign_request(method, path, body, timestamp)
    second = signer.sign_request(method, path, body, timestamp)
    deterministic = first == second
    if not deterministic:
        findings.append(
            "signatures differ across two signings of the same request: the "
            "nonce is randomised, which reopens the biased-nonce attack surface"
        )

    round_trip_ok = signer.verify(method, path, body, timestamp, first)
    if not round_trip_ok:
        findings.append("the signer's own verify rejected its own signature")

    public_key = base64.b64decode(signer.public_key_b64())
    signature = bytes.fromhex(first)
    independent_cofactorless = ed25519.verify(public_key, payload, signature, cofactored=False)
    independent_cofactored = ed25519.verify(public_key, payload, signature, cofactored=True)
    if not (independent_cofactorless and independent_cofactored):
        findings.append(
            "the signature did not verify under the independent Ed25519 "
            "implementation; the signer may be emitting a non-canonical form"
        )

    tampered = bytearray(signature)
    tampered[0] ^= 1
    tamper_rejected = not signer.verify(method, path, body, timestamp, bytes(tampered).hex())
    if not tamper_rejected:
        findings.append("a signature with a flipped byte was accepted")

    return SignerAudit(
        algorithm="Ed25519",
        deterministic=deterministic,
        round_trip_ok=round_trip_ok,
        independent_verify_cofactorless=independent_cofactorless,
        independent_verify_cofactored=independent_cofactored,
        tamper_rejected=tamper_rejected,
        findings=tuple(findings),
    )
