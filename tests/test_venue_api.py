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
