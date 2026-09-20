"""
Security response headers.

API-007. The headers are cheap, they are inert when nothing is wrong, and each
one closes a browser-side weakness that is otherwise the client's problem to
notice.

This is a JSON API with a separate dashboard front end, which shapes the
choices below more than a copied header list would:

- `Content-Security-Policy` is `default-src 'none'` rather than a permissive
  policy with exceptions. A JSON API loads nothing, so the correct policy is
  "load nothing", and a dashboard served from elsewhere is unaffected by this
  application's CSP.
- `Strict-Transport-Security` is emitted only over TLS. Sending HSTS over
  plain HTTP is ignored by browsers by specification, and emitting it anyway
  from a loopback-bound development server is how a developer ends up unable
  to reach `http://localhost` in that browser for the next year.
- `X-Frame-Options: DENY` and `frame-ancestors 'none'` say the same thing to
  browsers of different ages. Both, because the old one is still what some
  corporate browsers honour.

An existing header is never overwritten: a route that has deliberately set its
own `Cache-Control` knows something this middleware does not.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: Applied to every response. Values are deliberately strict; a route needing
#: something looser sets its own header and this middleware leaves it alone.
SECURITY_HEADERS: dict[str, str] = {
    # No sniffing. A JSON body that a browser decides is HTML is an XSS.
    "X-Content-Type-Options": "nosniff",
    # Clickjacking, for browsers old enough to need the header form.
    "X-Frame-Options": "DENY",
    # A JSON API loads nothing, so it may load nothing.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    # Do not leak the API path to a third party through a link.
    "Referrer-Policy": "no-referrer",
    # This API has no use for a camera, a microphone or geolocation.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    # An operator's browser must not cache a position or an equity curve.
    "Cache-Control": "no-store",
}

#: Emitted only when the request arrived over TLS. Two years, subdomains
#: included; preload is deliberately absent, because preloading is effectively
#: irreversible and that is the operator's decision, not this file's.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=63072000; includeSubDomains")


def apply_security_headers(headers: Any, *, is_secure: bool) -> None:
    """
    Set the declared headers on `headers`, leaving existing values alone.

    Takes the header mapping rather than the response so it can be tested
    without constructing a response object, and so a WebSocket handshake can
    use the same policy as an HTTP response.
    """
    for name, value in SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value
    if is_secure:
        name, value = HSTS_HEADER
        if name not in headers:
            headers[name] = value


class SecurityHeadersMiddleware:
    """
    Pure-ASGI middleware applying :data:`SECURITY_HEADERS`.

    ASGI rather than Starlette's `BaseHTTPMiddleware`: that base class wraps
    every response in a streaming task, which breaks the WebSocket route and
    adds a task per request to a service whose event loop is shared with the
    trading path.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        is_secure = scope.get("scheme") in ("https", "wss")

        async def _send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = _MutableRawHeaders(message.setdefault("headers", []))
                apply_security_headers(headers, is_secure=is_secure)
            await send(message)

        await self._app(scope, receive, _send)


class _MutableRawHeaders:
    """
    A mapping view over ASGI's list-of-byte-pairs header format.

    ASGI hands headers over as `[(b"name", b"value"), ...]`, which has no
    membership test and no case folding. Wrapping it keeps
    `apply_security_headers` written against an ordinary mapping rather than
    duplicating the byte handling at every call site.
    """

    def __init__(self, raw: list[tuple[bytes, bytes]]) -> None:
        self._raw = raw

    def __contains__(self, name: object) -> bool:
        wanted = str(name).lower().encode("latin-1")
        return any(key.lower() == wanted for key, _ in self._raw)

    def __setitem__(self, name: str, value: str) -> None:
        self._raw.append((name.encode("latin-1"), value.encode("latin-1")))
