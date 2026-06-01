"""
CORS validation middleware — prevents wildcard + credentials misconfiguration.

Runs at application startup (via validate_cors_config()) before
CORSMiddleware is added, so a misconfigured deployment fails fast
instead of silently leaking credentials to arbitrary origins.
"""

from __future__ import annotations

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def validate_cors_config(origins: list[str], allow_credentials: bool) -> None:
    """
    Assert that wildcard CORS origins are never combined with
    allow_credentials=True.

    FastAPI/Starlette silently accepts this combination; browsers reject
    it at runtime but the server still sends the header, leaking
    session state to arbitrary origins.

    Raises
    ------
    RuntimeError : on unsafe combination, halting startup.
    """
    if allow_credentials and "*" in origins:
        raise RuntimeError(
            "CORS misconfiguration: allow_credentials=True cannot be combined "
            "with wildcard origin '*'. Set explicit origins in API_CORS_ORIGINS."
        )
    log.info("cors.validated", origins=origins, allow_credentials=allow_credentials)
