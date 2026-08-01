"""
API authentication — API key validation for REST and WebSocket.

All routes and the WebSocket endpoint require the X-API-Key header
to match the API_SECRET_KEY environment variable.

Comparison uses hmac.compare_digest to prevent timing-oracle attacks.

Role resolution (src/api/access_control.py) is layered on top and is
strictly opt-in: with only API_SECRET_KEY configured — the deployment shape
that exists today — every authenticated caller resolves to
Role.TRADE_AUTHORIZING, which is byte-for-byte the current behaviour.
Setting the optional API_READONLY_KEY introduces a second, separate key
that authenticates successfully but resolves to Role.READ_ONLY, so
permission-gated endpoints reject it. Adding a key can therefore only ever
remove authority from a caller, never grant it.
"""

from __future__ import annotations

import hmac
import os

import structlog
from fastapi import HTTPException, WebSocket, status

from src.api.access_control import Role


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_HEADER_NAME = "x-api-key"

# Optional second key. Unset = single-key deployment = no role downgrade.
_READONLY_KEY_ENV = "API_READONLY_KEY"


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


def _get_readonly_key() -> str | None:
    """
    Return the optional read-only API key, or None when not configured.

    Raises RuntimeError when it is configured but unusable:
      - shorter than _MIN_KEY_LENGTH (a weak key is still a key), or
      - identical to API_SECRET_KEY, which would make the two roles
        indistinguishable. That is a silent privilege question, so it fails
        loudly at request time rather than resolving to a guessed role.
    """
    key = os.environ.get(_READONLY_KEY_ENV, "").strip()
    if not key:
        return None
    if len(key) < _MIN_KEY_LENGTH:
        raise RuntimeError(
            f"{_READONLY_KEY_ENV} is too short ({len(key)} chars). "
            f"Minimum {_MIN_KEY_LENGTH} characters required. "
            "Generate a strong key with: openssl rand -hex 32"
        )
    if hmac.compare_digest(key.encode("utf-8"), _get_configured_key().encode("utf-8")):
        raise RuntimeError(
            f"{_READONLY_KEY_ENV} is identical to API_SECRET_KEY — the read-only "
            "and trade-authorizing roles would be indistinguishable."
        )
    return key


def verify_api_key(api_key: str | None) -> Role:
    """
    Validate a client-supplied API key against the configured secret(s).

    Returns the Role the key authenticates as. Callers that only need
    authentication may ignore the return value — the raise-on-failure
    contract is unchanged.

    Raises HTTP 401 if missing or wrong, HTTP 503 if the server's own key
    configuration is unusable. Uses hmac.compare_digest to prevent timing
    attacks, and always compares against BOTH keys before deciding, so the
    response time does not reveal which key matched.
    """
    try:
        expected = _get_configured_key()
        readonly = _get_readonly_key()
    except RuntimeError as exc:
        log.critical("auth.key_not_configured", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication not configured.",
        ) from exc

    if api_key is not None:
        supplied = api_key.encode("utf-8")
        is_primary = hmac.compare_digest(supplied, expected.encode("utf-8"))
        is_readonly = readonly is not None and hmac.compare_digest(
            supplied, readonly.encode("utf-8")
        )
        if is_primary:
            return Role.TRADE_AUTHORIZING
        if is_readonly:
            return Role.READ_ONLY

    log.warning("auth.invalid_key")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def verify_ws_key(ws: WebSocket) -> Role:
    """
    Validate API key on a WebSocket upgrade request.

    Reads X-Api-Key from headers; closes the socket with 4401 if missing or
    invalid. The socket is a broadcast-only status stream, so a read-only
    key is accepted here — the returned Role is what callers must consult
    before honouring anything a client sends back over the socket.
    """
    try:
        expected = _get_configured_key()
        readonly = _get_readonly_key()
    except RuntimeError:
        await ws.close(code=4503)
        raise

    client_key = ws.headers.get(_HEADER_NAME, "")
    if client_key:
        supplied = client_key.encode("utf-8")
        is_primary = hmac.compare_digest(supplied, expected.encode("utf-8"))
        is_readonly = readonly is not None and hmac.compare_digest(
            supplied, readonly.encode("utf-8")
        )
        if is_primary:
            return Role.TRADE_AUTHORIZING
        if is_readonly:
            return Role.READ_ONLY

    log.warning("auth.ws_invalid_key", client=str(ws.client))
    await ws.close(code=4401)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
