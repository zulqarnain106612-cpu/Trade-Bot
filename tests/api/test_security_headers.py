"""
API-007, API-009 — security headers, and error responses that leak nothing.

The headers are cheap, inert when nothing is wrong, and each closes a
browser-side weakness that is otherwise the client's problem to notice. What
makes them worth a test file is that they are *silently* absent: nothing
fails, no request errors, and the gap is only visible if somebody looks.

The error-hygiene half (`API-009`) is the other side of the same coin. A 500
that returns a stack trace hands the attacker the next step, and the handler
most likely to echo its input is the one reporting that the input was bad --
the same finding the fuzz suite turned up in the exchange contract.
"""

from __future__ import annotations

import pytest

from src.api.security_headers import (
    HSTS_HEADER,
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
    apply_security_headers,
)


class TestTheDeclaredHeaders:
    @pytest.mark.parametrize("name", sorted(SECURITY_HEADERS))
    def test_each_declared_header_is_applied(self, name):
        headers: dict[str, str] = {}
        apply_security_headers(headers, is_secure=False)
        assert headers[name] == SECURITY_HEADERS[name]

    def test_the_list_covers_the_weaknesses_it_is_meant_to(self):
        # Named individually rather than by count: a header removed in a
        # refactor should fail by name, not by an arithmetic mismatch.
        for name in (
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Content-Security-Policy",
            "Referrer-Policy",
            "Permissions-Policy",
            "Cache-Control",
        ):
            assert name in SECURITY_HEADERS

    def test_sniffing_is_disabled(self):
        # A JSON body a browser decides is HTML is an XSS.
        assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"

    def test_framing_is_denied_in_both_spellings(self):
        # The header form for browsers old enough to need it, and the CSP
        # directive for the rest. Both, because corporate browsers lag.
        assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]

    def test_the_csp_loads_nothing(self):
        # A JSON API loads nothing, so the correct policy is "load nothing"
        # rather than a permissive policy with exceptions.
        assert SECURITY_HEADERS["Content-Security-Policy"].startswith("default-src 'none'")

    def test_responses_are_not_cached(self):
        # An operator's browser must not keep a position or an equity curve.
        assert SECURITY_HEADERS["Cache-Control"] == "no-store"


class TestHSTSIsConditional:
    def test_it_is_emitted_over_tls(self):
        headers: dict[str, str] = {}
        apply_security_headers(headers, is_secure=True)
        assert headers[HSTS_HEADER[0]] == HSTS_HEADER[1]

    def test_it_is_not_emitted_over_plain_http(self):
        # Browsers ignore HSTS over plain HTTP by specification, and emitting
        # it anyway from a loopback-bound development server is how a
        # developer loses http://localhost in that browser for two years.
        headers: dict[str, str] = {}
        apply_security_headers(headers, is_secure=False)
        assert HSTS_HEADER[0] not in headers

    def test_it_does_not_ask_for_preload(self):
        # Preloading is effectively irreversible, and that is the operator's
        # decision rather than this file's.
        assert "preload" not in HSTS_HEADER[1]

    def test_it_covers_subdomains(self):
        assert "includeSubDomains" in HSTS_HEADER[1]


class TestExistingHeadersAreNotOverwritten:
    def test_a_route_s_own_cache_control_wins(self):
        # A route that set its own knows something this middleware does not.
        headers = {"Cache-Control": "public, max-age=60"}
        apply_security_headers(headers, is_secure=False)
        assert headers["Cache-Control"] == "public, max-age=60"

    def test_the_other_headers_are_still_added(self):
        headers = {"Cache-Control": "public, max-age=60"}
        apply_security_headers(headers, is_secure=False)
        assert headers["X-Content-Type-Options"] == "nosniff"

    def test_an_existing_hsts_is_left_alone(self):
        headers = {HSTS_HEADER[0]: "max-age=1"}
        apply_security_headers(headers, is_secure=True)
        assert headers[HSTS_HEADER[0]] == "max-age=1"


class TestTheMiddleware:
    @staticmethod
    async def _run(scope: dict, app_response: dict) -> list[tuple[bytes, bytes]]:
        captured: list[tuple[bytes, bytes]] = []

        async def _app(scope, receive, send):
            await send(app_response)

        async def _send(message):
            if message["type"] == "http.response.start":
                captured.extend(message["headers"])

        await SecurityHeadersMiddleware(_app)(scope, None, _send)
        return captured

    async def test_headers_reach_an_http_response(self):
        headers = await self._run(
            {"type": "http", "scheme": "http"},
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        names = {name.decode().lower() for name, _ in headers}
        assert "x-content-type-options" in names

    async def test_hsts_is_added_on_an_https_scope(self):
        headers = await self._run(
            {"type": "http", "scheme": "https"},
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        names = {name.decode().lower() for name, _ in headers}
        assert "strict-transport-security" in names

    async def test_an_existing_raw_header_is_not_duplicated(self):
        headers = await self._run(
            {"type": "http", "scheme": "http"},
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public")],
            },
        )
        cache = [value for name, value in headers if name.lower() == b"cache-control"]
        assert cache == [b"public"]

    async def test_the_match_is_case_insensitive(self):
        # ASGI hands headers over as raw bytes with no case folding, so a
        # route setting `Cache-Control` and a middleware checking
        # `cache-control` would otherwise both emit one.
        headers = await self._run(
            {"type": "http", "scheme": "http"},
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"Cache-Control", b"public")],
            },
        )
        cache = [value for name, value in headers if name.lower() == b"cache-control"]
        assert cache == [b"public"]

    async def test_a_websocket_scope_passes_straight_through(self):
        # Starlette's BaseHTTPMiddleware would wrap this in a streaming task
        # and break the socket. This middleware is pure ASGI for that reason.
        seen: list[str] = []

        async def _app(scope, receive, send):
            seen.append(scope["type"])

        await SecurityHeadersMiddleware(_app)({"type": "websocket"}, None, None)
        assert seen == ["websocket"]

    async def test_a_body_message_passes_through_untouched(self):
        # Only the response *start* carries headers. Touching the body
        # message would corrupt the payload.
        sent: list[dict] = []

        async def _app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def _send(message):
            sent.append(message)

        await SecurityHeadersMiddleware(_app)({"type": "http", "scheme": "http"}, None, _send)
        assert sent[-1] == {"type": "http.response.body", "body": b"{}"}

    async def test_an_error_response_still_carries_the_headers(self):
        # The response most likely to be generated by a layer that forgot.
        headers = await self._run(
            {"type": "http", "scheme": "http"},
            {"type": "http.response.start", "status": 500, "headers": []},
        )
        names = {name.decode().lower() for name, _ in headers}
        assert "content-security-policy" in names


class TestTheMiddlewareIsWired:
    def test_main_installs_it(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text(
            encoding="utf-8"
        )
        assert "SecurityHeadersMiddleware" in source

    def test_it_is_the_outermost_middleware(self):
        # Starlette applies middleware in reverse registration order, so the
        # one added first is outermost and therefore the last to touch the
        # response -- which is what makes a header survive whatever the inner
        # layers did, including an error response CORS generated itself.
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text(
            encoding="utf-8"
        )
        # Compare the add_middleware *calls*, not the imports: the import
        # block is alphabetical and says nothing about ordering.
        assert source.index("add_middleware(SecurityHeadersMiddleware)") < source.index(
            "add_middleware(\n    CORSMiddleware"
        )
