"""
API authentication — API key validation for REST and WebSocket.

All routes and the WebSocket endpoint require the X-API-Key header
to match the API_SECRET_KEY environment variable.

Comparison uses hmac.compare_digest to prevent timing-oracle attacks.
"""

from __future__ import annotations

import hmac
import os

import structlog
from fastapi import HTTPException, WebSocket, status


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_HEADER_NAME = "x-api-key"


_MIN_KEY_LENGTH = 32  # 256-bit minimum — equivalent to openssl rand -hex 32


def _get_configured_key() -> str:
    """
    Return the server-side API key from environment.

    Raises RuntimeError on startup if:
      - key is not set, or
      - key is shorter than _MIN_KEY_LENGTH characters (weak key).

    Generate a strong key with: openssl rand -hex 32
    """
    key = os.environ.get("API_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "API_SECRET_KEY environment variable is not set. "
            "Generate one with: openssl rand -hex 32"
        )
    if len(key) < _MIN_KEY_LENGTH:
        raise RuntimeError(
            f"API_SECRET_KEY is too short ({len(key)} chars). "
            f"Minimum {_MIN_KEY_LENGTH} characters required. "
            "Generate a strong key with: openssl rand -hex 32"
        )
    return key


def verify_api_key(api_key: str | None) -> None:
    """
    Validate a client-supplied API key against the configured secret.

    Raises HTTP 401 if missing or wrong.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    try:
        expected = _get_configured_key()
    except RuntimeError as exc:
        log.critical("auth.key_not_configured", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication not configured.",
        ) from exc

    if api_key is None or not hmac.compare_digest(
        api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        log.warning("auth.invalid_key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def verify_ws_key(ws: WebSocket) -> None:
    """
    Validate API key on a WebSocket upgrade request.

    Reads X-Api-Key from headers; closes the socket with 4401
    if missing or invalid.
    """
    try:
        expected = _get_configured_key()
    except RuntimeError:
        await ws.close(code=4503)
        raise

    client_key = ws.headers.get(_HEADER_NAME, "")
    if not client_key or not hmac.compare_digest(
        client_key.encode("utf-8"), expected.encode("utf-8")
    ):
        log.warning("auth.ws_invalid_key", client=str(ws.client))
        await ws.close(code=4401)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
