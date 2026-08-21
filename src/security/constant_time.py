"""
Constant-time comparison utilities (Part II §9.4).

All secret comparisons must use these functions — not `==`.
Prevents Kocher (1996) timing attack: no branch on secret bytes,
no early-exit loops over secret data.
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
