"""
API-008, API-005 — authentication, and what happens when it cannot run.

The requirement that shapes this file:

> If authentication, authorization, rate limiting or logging is unavailable,
> the trading endpoints refuse rather than degrade to open.

An outage in the auth dependency must not make the trade endpoint anonymous.
That is the specific way a security control becomes a trading vulnerability,
and it is a failure nobody notices while it is happening — every request
succeeds.

The WebSocket half (`API-005`) is here because the socket's authentication is
the same decision made in a different place, and the place is where it goes
wrong: a browser cannot set a header on a WebSocket upgrade, so the fallback
that makes the dashboard work is also the weaker path and has to be
deliberate rather than accidental.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.access_control import Role
from src.api.auth import verify_api_key, verify_ws_key

#: 64 hex characters, as `openssl rand -hex 32` produces. The module refuses
#: anything shorter than 32: a weak key is still a key, and a deployment that
#: set one would authenticate happily.
PRIMARY = "a" * 64
READONLY = "b" * 64


@pytest.fixture
def keys(monkeypatch):
    """Configure both keys, distinct, as an operator would."""
    monkeypatch.setenv("API_SECRET_KEY", PRIMARY)
    monkeypatch.setenv("API_READONLY_KEY", READONLY)
    return PRIMARY, READONLY


class TestKeysMapToRoles:
    def test_the_primary_key_authorises_trading(self, keys):
        assert verify_api_key(PRIMARY) is Role.TRADE_AUTHORIZING

    def test_the_readonly_key_authorises_reading(self, keys):
        assert verify_api_key(READONLY) is Role.READ_ONLY

    def test_a_wrong_key_is_refused(self, keys):
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key("wrong")
        assert excinfo.value.status_code == 401

    def test_a_missing_key_is_refused(self, keys):
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key(None)
        assert excinfo.value.status_code == 401

    def test_an_empty_key_is_refused(self, keys):
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key("")
        assert excinfo.value.status_code == 401

    def test_a_prefix_of_the_real_key_is_refused(self, keys):
        with pytest.raises(HTTPException):
            verify_api_key(PRIMARY[:-1])

    def test_a_short_key_is_refused_as_a_configuration_fault(self, monkeypatch):
        # A weak key is still a key: a deployment that set one would
        # authenticate happily, so the length floor is enforced server-side
        # rather than left to whoever generated it.
        monkeypatch.setenv("API_SECRET_KEY", "short")
        monkeypatch.delenv("API_READONLY_KEY", raising=False)
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key("short")
        assert excinfo.value.status_code == 503

    def test_the_refusal_does_not_echo_the_supplied_key(self, keys):
        # A 401 that quotes what you sent is a 401 that puts a near-miss
        # credential in the server's log.
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key("almost-the-real-key")
        assert "almost-the-real-key" not in str(excinfo.value.detail)

    def test_the_refusal_advertises_the_scheme(self, keys):
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key(None)
        assert excinfo.value.headers.get("WWW-Authenticate") == "ApiKey"


class TestComparisonIsConstantTime:
    def test_both_keys_are_compared_before_deciding(self, keys):
        # The response time must not reveal *which* key matched, so the
        # implementation compares against both and decides afterwards.
        # Asserted on the source rather than by timing, because a timing
        # assertion in CI is a flaky test that eventually gets deleted.
        import inspect

        from src.api import auth

        source = inspect.getsource(auth.verify_api_key)
        assert source.index("is_readonly =") < source.index("if is_primary:")

    def test_it_uses_a_constant_time_primitive(self):
        import inspect

        from src.api import auth

        assert "compare_digest" in inspect.getsource(auth.verify_api_key)
        assert "compare_digest" in inspect.getsource(auth.verify_ws_key)


class TestAnUnconfiguredServerRefusesRatherThanOpens:
    def test_a_missing_key_configuration_is_a_503_not_a_pass(self, monkeypatch):
        # The failure this requirement is about: the control cannot run, so
        # the endpoint must refuse. A 503 says "not available"; degrading to
        # open would say "come in".
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        monkeypatch.delenv("API_READONLY_KEY", raising=False)
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key(PRIMARY)
        assert excinfo.value.status_code == 503

    def test_the_503_does_not_leak_the_configuration_problem(self, monkeypatch):
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        with pytest.raises(HTTPException) as excinfo:
            verify_api_key(PRIMARY)
        detail = str(excinfo.value.detail)
        assert "API_SECRET_KEY" not in detail
        assert PRIMARY not in detail


class TestTheTwoKeysMustDiffer:
    def test_identical_keys_are_refused(self, monkeypatch):
        # Otherwise the read-only and trade-authorizing roles are
        # indistinguishable, and every read-only holder can place a trade.
        monkeypatch.setenv("API_SECRET_KEY", PRIMARY)
        monkeypatch.setenv("API_READONLY_KEY", PRIMARY)
        with pytest.raises((RuntimeError, HTTPException)):
            verify_api_key(PRIMARY)


class TestTheWebSocketUpgrade:
    class _Socket:
        def __init__(self, headers=None, query=None):
            self.headers = headers or {}
            self.query_params = query or {}
            self.client = "test"
            self.closed_with: int | None = None

        async def close(self, code: int) -> None:
            self.closed_with = code

    async def test_a_header_key_authenticates(self, keys):
        socket = self._Socket(headers={"x-api-key": PRIMARY})
        assert await verify_ws_key(socket) is Role.TRADE_AUTHORIZING

    async def test_a_query_parameter_key_authenticates(self, keys):
        # Browsers cannot set headers on a WebSocket upgrade -- the API is
        # fixed by the WHATWG spec -- so the dashboard sends the key as a
        # query parameter. Accepting it is deliberate, and weaker: query
        # strings routinely appear in proxy and access logs where headers do
        # not.
        socket = self._Socket(query={"api_key": PRIMARY})
        assert await verify_ws_key(socket) is Role.TRADE_AUTHORIZING

    async def test_the_header_wins_over_the_query_parameter(self, keys):
        # A client that *can* set a header gets the stronger path.
        socket = self._Socket(headers={"x-api-key": PRIMARY}, query={"api_key": "wrong"})
        assert await verify_ws_key(socket) is Role.TRADE_AUTHORIZING

    async def test_a_readonly_key_opens_the_status_stream(self, keys):
        # The socket is a broadcast-only status stream, so read-only is
        # enough to receive -- and the returned Role is what the caller must
        # consult before honouring anything the client sends back.
        socket = self._Socket(headers={"x-api-key": READONLY})
        assert await verify_ws_key(socket) is Role.READ_ONLY

    async def test_an_unauthenticated_socket_is_closed(self, keys):
        socket = self._Socket()
        # HTTPException specifically: the close code is checked below, and a
        # blind `Exception` would also pass if the helper crashed before it
        # ever closed the socket.
        with pytest.raises(HTTPException):
            await verify_ws_key(socket)
        assert socket.closed_with == 4401

    async def test_a_wrong_key_closes_the_socket(self, keys):
        socket = self._Socket(headers={"x-api-key": "wrong"})
        # HTTPException specifically: the close code is checked below, and a
        # blind `Exception` would also pass if the helper crashed before it
        # ever closed the socket.
        with pytest.raises(HTTPException):
            await verify_ws_key(socket)
        assert socket.closed_with == 4401

    async def test_an_unconfigured_server_closes_the_socket(self, monkeypatch):
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        socket = self._Socket(headers={"x-api-key": PRIMARY})
        # RuntimeError, not HTTPException: a server with no key configured is
        # a deployment fault rather than a rejected caller, and the two must
        # stay distinguishable -- the socket is still closed, with 4503.
        with pytest.raises(RuntimeError):
            await verify_ws_key(socket)
        assert socket.closed_with == 4503
