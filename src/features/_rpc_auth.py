"""HTTP Basic auth header for the bitcoind JSON-RPC clients.

``aiohttp.BasicAuth`` is deprecated and removed in aiohttp 4.0, and this
project turns ``DeprecationWarning`` from ``src.*`` into an error, so calling
it aborted every live RPC request.  Build the header ourselves instead.
"""

from __future__ import annotations

import base64

__all__ = ["basic_auth_header"]


def basic_auth_header(user: str, password: str) -> dict[str, str]:
    """Return an ``Authorization`` header mapping for *user* / *password*."""
    import aiohttp

    encoder = getattr(aiohttp, "encode_basic_auth", None)
    if encoder is not None:
        return {"Authorization": encoder(user, password)}
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}
