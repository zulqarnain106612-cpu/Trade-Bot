"""
Ed25519 deterministic API request signing (Part II §9.1).

Replaces HMAC-SHA256 signing with Ed25519 (from cryptography package).
Ed25519 uses RFC 6979-equivalent deterministic nonce internally:
  - No random k → immune to entropy failures (Android 2013 / PS3 2010)
  - Deterministic: same message + key → same signature

Attack defended:
  - ECDSA k-reuse → full private key recovery (A1 from cross-layer table)
  - Kocher timing attack → Ed25519 complete-addition-law, constant time
"""

from __future__ import annotations

import base64
import os


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
