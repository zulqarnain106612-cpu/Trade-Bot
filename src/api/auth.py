"""
API authentication — API key validation for REST and WebSocket.

All routes and the WebSocket endpoint require the X-API-Key header
to match one of two configured keys:

  - ``API_SECRET_KEY``   → Role.TRADE_AUTHORIZING (full access)
  - ``API_READONLY_KEY`` → Role.READ_ONLY (optional; view-only access)

``API_READONLY_KEY`` is optional. When unset, the only accepted key is
``API_SECRET_KEY`` and every authenticated caller is trade-authorizing —
identical to the single-key model that preceded this. When set it must
satisfy the same length floor and must differ from ``API_SECRET_KEY``,
otherwise startup fails closed rather than silently collapsing the two
roles into one.

Comparison uses hmac.compare_digest to prevent timing-oracle attacks, and
both keys are always compared (no short-circuit) so the response time does
not reveal which key matched.
"""

from __future__ import annotations

import hmac
import os

import structlog
from fastapi import HTTPException, WebSocket, status

from src.api.access_control import Role


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


def _key_matches(supplied: bytes, key: str | None) -> bool:
    """Constant-time comparison; an unconfigured (None) key never matches."""
    return key is not None and hmac.compare_digest(supplied, key.encode("utf-8"))


def _get_readonly_key() -> str | None:
    """
    Return the optional read-only API key from environment, or None.

    Raises RuntimeError if the key is set but unusable:
      - shorter than _MIN_KEY_LENGTH characters (weak key), or
      - identical to API_SECRET_KEY (would silently grant full access to a
        caller the operator believes is read-only).
    """
    key = os.environ.get("API_READONLY_KEY", "").strip()
    if not key:
        return None
    if len(key) < _MIN_KEY_LENGTH:
        raise RuntimeError(
            f"API_READONLY_KEY is too short ({len(key)} chars). "
            f"Minimum {_MIN_KEY_LENGTH} characters required. "
            "Generate a strong key with: openssl rand -hex 32"
        )
    if hmac.compare_digest(key.encode("utf-8"), _get_configured_key().encode("utf-8")):
        raise RuntimeError(
            "API_READONLY_KEY must differ from API_SECRET_KEY — identical keys "
            "would grant trade-authorizing access to read-only clients."
        )
    return key


def resolve_role(api_key: str | None) -> Role:
    """
    Authenticate a client-supplied API key and return the role it carries.

    Raises HTTP 401 if missing or matching neither configured key, and
    HTTP 503 if the server keys are missing/misconfigured.

    Both comparisons run unconditionally so the elapsed time does not leak
    which of the two keys was presented.
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

    supplied = (api_key or "").encode("utf-8")
    is_trade = _key_matches(supplied, expected)
    is_readonly = _key_matches(supplied, readonly)

    if api_key is None or not (is_trade or is_readonly):
        log.warning("auth.invalid_key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return Role.TRADE_AUTHORIZING if is_trade else Role.READ_ONLY


def verify_api_key(api_key: str | None) -> None:
    """
    Validate a client-supplied API key against the configured secret(s).

    Raises HTTP 401 if missing or wrong. Accepts either configured key —
    authorization (what the role may *do*) is enforced separately by the
    permission dependencies in src/api/main.py.
    """
    resolve_role(api_key)


async def verify_ws_key(ws: WebSocket) -> None:
    """
    Validate API key on a WebSocket upgrade request.

    Reads X-Api-Key from headers; closes the socket with 4401
    if missing or invalid. Either configured key is accepted — the socket
    is a read-only broadcast stream, so READ_ONLY suffices.
    """
    try:
        expected = _get_configured_key()
        readonly = _get_readonly_key()
    except RuntimeError:
        await ws.close(code=4503)
        raise

    client_key = ws.headers.get(_HEADER_NAME, "")
    supplied = client_key.encode("utf-8")
    is_trade = _key_matches(supplied, expected)
    is_readonly = _key_matches(supplied, readonly)
    if not client_key or not (is_trade or is_readonly):
        log.warning("auth.ws_invalid_key", client=str(ws.client))
        await ws.close(code=4401)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
