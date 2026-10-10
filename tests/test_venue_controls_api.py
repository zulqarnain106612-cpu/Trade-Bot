"""
VEN-001 -- the venue control endpoints keep every existing guard.

Connect, disconnect and verify are state changes on the trading path, so they
carry exactly what the existing controls carry: the API key, a permission a
read-only key does not hold, the operator second factor, a rate limit, an
audit record and an out-of-band push to every dashboard. The fetcher is a
fake; nothing here can reach an exchange or place an order.

Decides:
  - VEN-001 -- venue connections are controlled per venue, with honest state
"""

from __future__ import annotations

from typing import Any

import pytest

from src.config import EXCHANGE_BINANCE, EXCHANGE_OKX
from src.data.fetcher import VenueDisconnectRefused

_KEY = "test-key-" + "a" * 32  # pragma: allowlist secret
_READONLY = "readonly-key-" + "r" * 32  # pragma: allowlist secret
_SECRET = "operator-secret-" + "b" * 32  # pragma: allowlist secret


def _status(state: str, account: str = "unverified") -> dict[str, Any]:
    return {
        "available": state == "connected",
        "error": None,
        "state": state,
        "changed_at": None,
        "operator_disconnected": state == "disconnected",
        "testnet": True,
        "credentials": "configured",
        "account": {"state": account, "error": None, "checked_at": None},
    }


class FakeFetcher:
    def __init__(self) -> None:
        self.status = {EXCHANGE_BINANCE: _status("connected"), EXCHANGE_OKX: _status("failed")}
        self.calls: list[tuple[str, str]] = []
        self.reconnect_result = True
        self.refuse_disconnect = False

    def venue_status(self) -> dict[str, dict[str, Any]]:
        return self.status

    async def reconnect(self, venue: str) -> bool:
        self.calls.append(("reconnect", venue))
        self.status[venue] = _status("connected" if self.reconnect_result else "failed")
        return self.reconnect_result

    async def disconnect(self, venue: str) -> None:
        self.calls.append(("disconnect", venue))
        if self.refuse_disconnect:
            raise VenueDisconnectRefused(f"{venue} is the only connected venue")
        self.status[venue] = _status("disconnected", "unavailable")

    async def verify_account(self, venue: str) -> str:
        self.calls.append(("verify", venue))
        self.status[venue] = _status("connected", "authenticated")
        return "authenticated"


class FakeStorage:
    def __init__(self) -> None:
        self.audit: list[dict[str, Any]] = []

    async def insert_audit_event(self, **event: Any) -> None:
        self.audit.append(event)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", _KEY)
    monkeypatch.setenv("API_READONLY_KEY", _READONLY)
    monkeypatch.setenv("OPERATOR_SECRET", _SECRET)
    from fastapi.testclient import TestClient

    from src.api import main as api_main

    fetcher = FakeFetcher()
    storage = FakeStorage()
    pushed: list[dict[str, Any]] = []

    async def broadcast(payload: dict[str, Any], topic: str | None = None) -> int:
        pushed.append(payload)
        return 0

    api_main._state.ready = True
    orchestrator = type("O", (), {"_fetcher": fetcher})()
    monkeypatch.setattr(api_main, "require_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(api_main._state, "storage", storage, raising=False)
    monkeypatch.setattr(api_main._state, "broadcast", broadcast)
    monkeypatch.setattr(api_main._state, "_endpoint_hits", {})
    client = TestClient(api_main.app)
    client.headers.update({"x-api-key": _KEY})
    client.fetcher = fetcher  # type: ignore[attr-defined]
    client.storage = storage  # type: ignore[attr-defined]
    client.pushed = pushed  # type: ignore[attr-defined]
    client.api_main = api_main  # type: ignore[attr-defined]
    return client


def _body(**overrides: Any) -> dict[str, Any]:
    return {"operator": "alice", "operator_secret": _SECRET, **overrides}


class TestConnect:
    def test_connect_reconnects_then_verifies_and_records_it(self, api):
        response = api.post(f"/venues/{EXCHANGE_OKX}/connect", json=_body())
        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is True and body["state"] == "connected"
        assert body["account"]["state"] == "authenticated"
        assert api.fetcher.calls == [("reconnect", EXCHANGE_OKX), ("verify", EXCHANGE_OKX)]
        (event,) = api.storage.audit
        assert event["event_type"] == "venue_connect" and event["operator"] == "alice"
        assert event["details"]["venue"] == EXCHANGE_OKX
        assert api.pushed[-1]["type"] == "venue_changed" and api.pushed[-1]["venue"] == EXCHANGE_OKX

    def test_a_failed_connect_skips_the_account_check(self, api):
        api.fetcher.reconnect_result = False
        body = api.post(f"/venues/{EXCHANGE_OKX}/connect", json=_body()).json()
        assert body["connected"] is False and body["state"] == "failed"
        assert api.fetcher.calls == [("reconnect", EXCHANGE_OKX)]


class TestDisconnect:
    def test_disconnect_closes_one_venue(self, api):
        body = api.post(f"/venues/{EXCHANGE_OKX}/disconnect", json=_body()).json()
        assert body["disconnected"] is True and body["state"] == "disconnected"
        assert api.storage.audit[-1]["event_type"] == "venue_disconnect"

    def test_the_last_venue_is_refused_with_409(self, api):
        api.fetcher.refuse_disconnect = True
        response = api.post(f"/venues/{EXCHANGE_BINANCE}/disconnect", json=_body())
        assert response.status_code == 409
        assert "only connected venue" in response.json()["detail"]
        assert api.storage.audit == []


class TestVerify:
    def test_verify_rechecks_the_account(self, api):
        body = api.post(f"/venues/{EXCHANGE_BINANCE}/verify", json=_body()).json()
        assert body["account"]["state"] == "authenticated"
        assert api.storage.audit[-1]["details"]["account"] == "authenticated"


ACTIONS = ["connect", "disconnect", "verify", "reconnect"]


class TestEveryControlKeepsTheGuards:
    @pytest.mark.parametrize("action", ACTIONS)
    def test_an_unknown_venue_is_404_before_anything_else(self, api, action):
        response = api.post(f"/venues/kraken/{action}", json=_body())
        assert response.status_code == 404
        assert api.fetcher.calls == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_the_operator_secret_is_required(self, api, action):
        body = _body(operator_secret="wrong")
        assert api.post(f"/venues/{EXCHANGE_OKX}/{action}", json=body).status_code == 401
        assert api.fetcher.calls == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_an_unconfigured_secret_is_503_not_an_open_door(self, api, action, monkeypatch):
        monkeypatch.setenv("OPERATOR_SECRET", "")
        assert api.post(f"/venues/{EXCHANGE_OKX}/{action}", json=_body()).status_code == 503
        assert api.fetcher.calls == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_a_read_only_key_is_forbidden(self, api, action):
        response = api.post(
            f"/venues/{EXCHANGE_OKX}/{action}", json=_body(), headers={"x-api-key": _READONLY}
        )
        assert response.status_code == 403
        assert api.fetcher.calls == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_no_key_is_unauthorized(self, api, action):
        api.headers.pop("x-api-key")
        assert api.post(f"/venues/{EXCHANGE_OKX}/{action}", json=_body()).status_code == 401

    @pytest.mark.parametrize(
        "body",
        [
            {"operator": "alice"},
            _body(operator="bad name!"),
            _body(operator_secret="x" * 300),
            _body(operator_secret=""),
        ],
    )
    def test_malformed_bodies_are_rejected(self, api, body):
        assert api.post(f"/venues/{EXCHANGE_OKX}/connect", json=body).status_code == 422

    def test_the_rate_limit_applies(self, api, monkeypatch):
        monkeypatch.setattr(api.api_main._state, "_ENDPOINT_LIMIT", 1)
        assert api.post(f"/venues/{EXCHANGE_OKX}/verify", json=_body()).status_code == 200
        assert api.post(f"/venues/{EXCHANGE_OKX}/verify", json=_body()).status_code == 429

    def test_an_action_without_an_audit_store_still_answers(self, api, monkeypatch):
        monkeypatch.setattr(api.api_main._state, "storage", None, raising=False)
        assert api.post(f"/venues/{EXCHANGE_OKX}/verify", json=_body()).status_code == 200

    def test_reconnect_still_accepts_its_original_body(self, api):
        response = api.post(f"/venues/{EXCHANGE_OKX}/reconnect", json={"operator_secret": _SECRET})
        assert response.status_code == 200
        assert response.json()["reconnected"] is True
        assert api.storage.audit[-1]["operator"] == "operator"


def test_the_status_endpoint_still_reads_without_a_permission(api):
    api.headers.update({"x-api-key": _READONLY})
    body = api.get("/venues").json()
    assert body["available"] == [EXCHANGE_BINANCE]
    assert body["venues"][EXCHANGE_OKX]["account"]["state"] == "unverified"
