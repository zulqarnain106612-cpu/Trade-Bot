"""
The venue control surface: what is up, and how to bring one back.

REG-0014. Because a venue can now be down while the bot runs, two things have
to be reachable without a restart -- the state, and the recovery. These tests
hold that surface: the reason a venue is down survives all the way to the
response (an operator has to tell a network fault from a 451 eligibility
block), and reconnecting carries the same second factor as every other control
on the trading path.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.config import EXCHANGE_BINANCE, EXCHANGE_OKX

_TEST_SECRET = "operator-secret-" + "b" * 32  # pragma: allowlist secret
_WRONG_SECRET = "wrong-secret-" + "c" * 32  # pragma: allowlist secret
_GEO_BLOCK = "ExchangeNotAvailable: binance GET .../exchangeInfo 451 Unknown"


class _FakeFetcher:
    def __init__(self) -> None:
        self.status: dict[str, dict[str, Any]] = {
            EXCHANGE_BINANCE: {"available": False, "error": _GEO_BLOCK},
            EXCHANGE_OKX: {"available": True, "error": None},
        }
        self.reconnect_calls: list[str] = []
        self.reconnect_result = True

    def venue_status(self) -> dict[str, dict[str, Any]]:
        return self.status

    async def reconnect(self, venue: str) -> bool:
        if venue not in (EXCHANGE_BINANCE, EXCHANGE_OKX):
            raise ValueError(f"unknown venue: {venue}")
        self.reconnect_calls.append(venue)
        if self.reconnect_result:
            self.status[venue] = {"available": True, "error": None}
        return self.reconnect_result


class _FakeOrchestrator:
    def __init__(self, fetcher: _FakeFetcher) -> None:
        self._fetcher = fetcher


@pytest.fixture()
def api_client(monkeypatch):
    os.environ["API_SECRET_KEY"] = "test-key-" + "a" * 32  # pragma: allowlist secret
    os.environ["OPERATOR_SECRET"] = _TEST_SECRET

    from fastapi.testclient import TestClient

    from src.api import main as api_main

    fetcher = _FakeFetcher()
    api_main._state.ready = True
    monkeypatch.setattr(api_main, "require_orchestrator", lambda: _FakeOrchestrator(fetcher))

    client = TestClient(api_main.app)
    client.headers.update({"x-api-key": os.environ["API_SECRET_KEY"]})
    client.fetcher = fetcher  # type: ignore[attr-defined]
    return client


class TestVenueStatusIsReadable:
    def test_it_reports_which_venues_are_up(self, api_client):
        body = api_client.get("/venues").json()

        assert body["available"] == [EXCHANGE_OKX]
        assert body["degraded"] is True

    def test_the_reason_a_venue_is_down_reaches_the_response(self, api_client):
        """
        The whole point of the field: "unavailable" does not tell an operator
        whether to retry, and a 451 is not worth retrying from that network.
        """
        body = api_client.get("/venues").json()

        assert "451" in body["venues"][EXCHANGE_BINANCE]["error"]

    def test_it_needs_the_api_key(self, api_client):
        api_client.headers.pop("x-api-key")
        assert api_client.get("/venues").status_code == 401

    def test_a_fully_healthy_bot_is_not_degraded(self, api_client):
        api_client.fetcher.status[EXCHANGE_BINANCE] = {"available": True, "error": None}

        body = api_client.get("/venues").json()

        assert body["degraded"] is False
        assert body["available"] == sorted([EXCHANGE_BINANCE, EXCHANGE_OKX])


class TestReconnectCarriesTheSecondFactor:
    def test_a_valid_operator_secret_reconnects(self, api_client):
        response = api_client.post(
            f"/venues/{EXCHANGE_BINANCE}/reconnect",
            json={"operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 200
        assert response.json()["reconnected"] is True
        assert response.json()["available"] is True
        assert api_client.fetcher.reconnect_calls == [EXCHANGE_BINANCE]

    def test_a_wrong_operator_secret_is_rejected_and_reconnects_nothing(self, api_client):
        response = api_client.post(
            f"/venues/{EXCHANGE_BINANCE}/reconnect",
            json={"operator_secret": _WRONG_SECRET},
        )

        assert response.status_code == 401
        assert api_client.fetcher.reconnect_calls == []

    def test_an_unconfigured_operator_secret_is_503_not_an_open_door(self, api_client):
        os.environ["OPERATOR_SECRET"] = ""
        try:
            response = api_client.post(
                f"/venues/{EXCHANGE_BINANCE}/reconnect",
                json={"operator_secret": _TEST_SECRET},
            )
        finally:
            os.environ["OPERATOR_SECRET"] = _TEST_SECRET

        assert response.status_code == 503
        assert api_client.fetcher.reconnect_calls == []

    def test_an_unknown_venue_is_404(self, api_client):
        response = api_client.post(
            "/venues/kraken/reconnect", json={"operator_secret": _TEST_SECRET}
        )

        assert response.status_code == 404
        assert "unknown venue" in response.json()["detail"]

    def test_a_failed_reconnect_reports_failure_rather_than_pretending(self, api_client):
        api_client.fetcher.reconnect_result = False

        response = api_client.post(
            f"/venues/{EXCHANGE_BINANCE}/reconnect",
            json={"operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 200
        assert response.json()["reconnected"] is False
        assert response.json()["available"] is False


class TestSetControlCarriesTheSecondFactor:
    """
    POST /controls/{name} moves live trading parameters, so it is gated like
    /execution-mode and /risk-controls. The endpoint's job beyond that is to
    translate a ControlWriteError into the status it carries.
    """

    def test_a_wrong_operator_secret_is_rejected(self, api_client):
        response = api_client.post(
            "/controls/execution_mode",
            json={"value": "manual", "operator": "alice", "operator_secret": _WRONG_SECRET},
        )

        assert response.status_code == 401

    def test_an_unconfigured_operator_secret_is_503(self, api_client):
        os.environ["OPERATOR_SECRET"] = ""
        try:
            response = api_client.post(
                "/controls/execution_mode",
                json={"value": "manual", "operator": "alice", "operator_secret": _TEST_SECRET},
            )
        finally:
            os.environ["OPERATOR_SECRET"] = _TEST_SECRET

        assert response.status_code == 503

    def test_a_protected_parameter_is_403(self, api_client):
        response = api_client.post(
            "/controls/risk.kelly_multiplier",
            json={"value": 2.0, "operator": "alice", "operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 403

    def test_an_unknown_control_is_404(self, api_client):
        response = api_client.post(
            "/controls/nope",
            json={"value": 1, "operator": "alice", "operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 404

    def test_an_out_of_range_value_is_422(self, api_client):
        response = api_client.post(
            "/controls/risk_controls.stop_loss_pct",
            json={"value": 999.0, "operator": "alice", "operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 422

    def test_a_valid_write_applies_and_says_so(self, api_client):
        response = api_client.post(
            "/controls/risk_controls.stop_loss_pct",
            json={"value": 3.5, "operator": "alice", "operator_secret": _TEST_SECRET},
        )

        assert response.status_code == 200
        assert response.json() == {
            "applied": True,
            "name": "risk_controls.stop_loss_pct",
            "value": 3.5,
        }


class TestAControlChangeIsPushedOutOfBand:
    """
    Each connection pushes its own heartbeat, so a control moved between ticks
    is invisible until the next one -- long enough for an operator to move a
    slider, see nothing happen, and move it again. The write broadcasts.
    """

    class _FakeWS:
        def __init__(self, fails: bool = False) -> None:
            self.sent: list[str] = []
            self._fails = fails

        async def send_text(self, message: str) -> None:
            if self._fails:
                raise RuntimeError("client went away")
            self.sent.append(message)

    async def test_the_new_value_reaches_every_client(self):
        from src.api import main as api_main

        first, second = self._FakeWS(), self._FakeWS()
        for client in (first, second):
            api_main._state._ws_clients.add(client)
        try:
            delivered = await api_main._state.broadcast({"type": "control_changed", "value": 3.5})
        finally:
            for client in (first, second):
                api_main._state._ws_clients.discard(client)

        assert delivered == 2
        assert "control_changed" in first.sent[0]
        assert "control_changed" in second.sent[0]

    async def test_a_dead_client_is_dropped_and_does_not_stop_the_others(self):
        """
        The broadcast runs *after* the write has been applied, so a dashboard
        that has gone away must not make the write look failed, and must not
        deprive the live clients of the message.
        """
        from src.api import main as api_main

        dead, alive = self._FakeWS(fails=True), self._FakeWS()
        for client in (dead, alive):
            api_main._state._ws_clients.add(client)
        try:
            delivered = await api_main._state.broadcast({"type": "control_changed"})

            assert delivered == 1
            assert alive.sent, "a dead peer must not cost a live client its message"
            assert dead not in api_main._state.ws_clients
        finally:
            for client in (dead, alive):
                api_main._state._ws_clients.discard(client)

    async def test_no_clients_is_not_an_error(self):
        from src.api import main as api_main

        assert await api_main._state.broadcast({"type": "control_changed"}) == 0

    def test_a_successful_write_broadcasts(self, api_client):
        from src.api import main as api_main

        listener = self._FakeWS()
        api_main._state._ws_clients.add(listener)
        try:
            response = api_client.post(
                "/controls/risk_controls.stop_loss_pct",
                json={"value": 2.5, "operator": "alice", "operator_secret": _TEST_SECRET},
            )
        finally:
            api_main._state._ws_clients.discard(listener)

        assert response.status_code == 200
        assert len(listener.sent) == 1
        assert "risk_controls.stop_loss_pct" in listener.sent[0]

    def test_a_rejected_write_broadcasts_nothing(self, api_client):
        """A 403 changed no state, so announcing a change would be a lie."""
        from src.api import main as api_main

        listener = self._FakeWS()
        api_main._state._ws_clients.add(listener)
        try:
            response = api_client.post(
                "/controls/risk.kelly_multiplier",
                json={"value": 2.0, "operator": "alice", "operator_secret": _TEST_SECRET},
            )
        finally:
            api_main._state._ws_clients.discard(listener)

        assert response.status_code == 403
        assert listener.sent == []
