"""
CORS validation middleware — prevents wildcard + credentials misconfiguration
and rejects unsafe origin values ("null", bare IPs without scheme, etc.).
"""

from __future__ import annotations

import re

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Accepted origin pattern: http(s)://hostname(:port)
_ORIGIN_RE = re.compile(r"^https?://[a-zA-Z0-9._-]+(:\d+)?$")


def validate_cors_config(origins: list[str], allow_credentials: bool) -> None:
    """
    Validate CORS origins before adding middleware.

    Rules enforced:
      1. Wildcard '*' + allow_credentials=True is rejected (browser blocks it,
         but server would still emit the header — leaks session state).
      2. The string "null" is rejected — some browsers send 'null' as the Origin
         for sandboxed iframes and file:// pages, which would grant them access.
      3. Every origin must match https?://hostname(:port) — rejects bare IPs,
         schemeless strings, and other malformed values.

    Raises RuntimeError on any unsafe combination, halting startup.
    """
    if allow_credentials and "*" in origins:
        raise RuntimeError(
            "CORS misconfiguration: allow_credentials=True cannot be combined "
            "with wildcard origin '*'. Set explicit origins in API_CORS_ORIGINS."
        )

    for origin in origins:
        if origin.lower() == "null":
            raise RuntimeError(
                f"CORS misconfiguration: origin {origin!r} is not allowed. "
                "'null' origin enables access from sandboxed iframes and file:// pages."
            )
        if not _ORIGIN_RE.match(origin):
            raise RuntimeError(
                f"CORS misconfiguration: origin {origin!r} does not match "
                "the required format 'http(s)://hostname(:port)'. "
                "Check API_CORS_ORIGINS in your .env."
            )

    log.info("cors.validated", origins=origins, allow_credentials=allow_credentials)
