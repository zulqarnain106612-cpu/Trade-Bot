"""
Constant-time comparison utilities (Part II §9.4).

All secret comparisons must use these functions — not `==`.
Prevents Kocher (1996) timing attack: no branch on secret bytes,
no early-exit loops over secret data.

post_quantum posture (LAW12):
  Nothing here needs a PQC migration, and the reason is not "it is only a
  comparison". This module contains no asymmetric cryptography at all --
  hmac.compare_digest is a byte-wise equality check, so there is no
  discrete-log or factoring problem for Shor's algorithm to solve.

  Grover's algorithm is the only quantum result that touches this code
  path, and it applies to the secrets being compared rather than to the
  comparison: it halves the effective search space, leaving a 256-bit token
  at ~128 bits, which is the accepted post-quantum floor. The requirement
  that follows is on the callers -- keep compared secrets at 256 bits --
  not on this file.

  The gate flags it because its scope is src/security/, not because it has
  quantum-fragile primitives.
"""

from __future__ import annotations

import hmac


def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison via HMAC-digest."""
    return hmac.compare_digest(a.encode(), b.encode())


def safe_compare_bytes(a: bytes, b: bytes) -> bool:
    """Constant-time bytes comparison."""
    return hmac.compare_digest(a, b)


def safe_compare_tokens(provided: str, stored: str) -> bool:
    """API key / auth token validation — always constant-time."""
    return hmac.compare_digest(
        provided.encode("utf-8"),
        stored.encode("utf-8"),
    )
