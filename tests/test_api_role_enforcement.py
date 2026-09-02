"""
Role enforcement on the FastAPI surface.

Covers the `requires(...)` dependency factory in src/api/main.py: a read-only
key authenticates (200/401 unchanged) but is denied 403 on the trade- and
mode-changing routes, while the trade-authorizing key passes through.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.access_control import Permission, Role

_TRADE_KEY = "t" * 32
_READ_KEY = "r" * 32


@pytest.fixture
def role_env():
    """Both keys configured — the only mode in which roles actually differ."""
    with patch.dict(
        os.environ,
        {"API_SECRET_KEY": _TRADE_KEY, "API_READONLY_KEY": _READ_KEY},
    ):
        yield


# ---------------------------------------------------------------------------
# requires() — dependency factory in isolation
# ---------------------------------------------------------------------------


def test_resolve_role_maps_trade_key(role_env) -> None:
    from src.api.main import resolve_role

    assert resolve_role(_TRADE_KEY) is Role.TRADE_AUTHORIZING


def test_resolve_role_maps_readonly_key(role_env) -> None:
    from src.api.main import resolve_role

    assert resolve_role(_READ_KEY) is Role.READ_ONLY


def test_resolve_role_rejects_unknown_key_with_401(role_env) -> None:
    from src.api.main import resolve_role

    with pytest.raises(HTTPException) as exc_info:
        resolve_role("z" * 32)
    assert exc_info.value.status_code == 401


def test_requires_allows_trade_authorizing_role() -> None:
    from src.api.main import requires

    requires(Permission.CHANGE_EXECUTION_MODE)(Role.TRADE_AUTHORIZING)  # no raise


def test_requires_denies_read_only_role_with_403() -> None:
    from src.api.main import requires

    with pytest.raises(HTTPException) as exc_info:
        requires(Permission.CHANGE_EXECUTION_MODE)(Role.READ_ONLY)
    assert exc_info.value.status_code == 403
    assert "lacks permission" in str(exc_info.value.detail)


def test_requires_denies_read_only_role_on_approve_trade() -> None:
    from src.api.main import requires

    with pytest.raises(HTTPException) as exc_info:
        requires(Permission.APPROVE_TRADE)(Role.READ_ONLY)
    assert exc_info.value.status_code == 403


def test_requires_allows_read_only_role_for_view_permissions() -> None:
    from src.api.main import requires

    requires(Permission.VIEW_STATUS)(Role.READ_ONLY)  # no raise
    requires(Permission.VIEW_TRADES)(Role.READ_ONLY)  # no raise


def test_single_key_deployment_resolves_as_trade_authorizing() -> None:
    """Without API_READONLY_KEY the one key is full access, as before RBAC."""
    from src.api.main import resolve_role

    with patch.dict(os.environ, {"API_SECRET_KEY": _TRADE_KEY}):
        os.environ.pop("API_READONLY_KEY", None)
        assert resolve_role(_TRADE_KEY) is Role.TRADE_AUTHORIZING


# ---------------------------------------------------------------------------
# End-to-end through the router
# ---------------------------------------------------------------------------


@pytest.fixture
def wired_state(role_env):
    from src.api.main import AppState

    state = AppState()
    state.ready = True
    state.storage = AsyncMock()
    orch = MagicMock()
    orch._executor = MagicMock()
    state.orchestrator = orch
    with patch("src.api.main._state", state):
        yield state


def _client() -> TestClient:
    from src.api.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_execution_mode_route_forbids_readonly_key(wired_state) -> None:
    resp = _client().post(
        "/execution-mode",
        headers={"x-api-key": _READ_KEY},
        json={"mode": "manual", "operator": "alice", "operator_secret": "s" * 32},
    )
    assert resp.status_code == 403


def test_risk_controls_route_forbids_readonly_key(wired_state) -> None:
    resp = _client().post(
        "/risk-controls",
        headers={"x-api-key": _READ_KEY},
        json={"operator": "alice", "operator_secret": "s" * 32},
    )
    assert resp.status_code == 403


def test_self_tuning_pause_forbids_readonly_key(wired_state) -> None:
    resp = _client().post(
        "/self-tuning/pause",
        headers={"x-api-key": _READ_KEY},
        json={"operator": "alice", "operator_secret": "s" * 32},
    )
    assert resp.status_code == 403


def test_resolve_approval_forbids_readonly_key(wired_state) -> None:
    resp = _client().post(
        "/approvals/00000000-0000-0000-0000-000000000000/resolve",
        headers={"x-api-key": _READ_KEY},
        json={"approved": True, "operator": "alice", "operator_secret": "s" * 32},
    )
    assert resp.status_code == 403


def test_readonly_key_still_reaches_a_get_route(wired_state) -> None:
    """403 must be specific to the mutating routes, not blanket denial."""
    resp = _client().get("/equity", headers={"x-api-key": _READ_KEY})
    assert resp.status_code != 403


def test_unknown_key_on_mutating_route_is_401_not_403(wired_state) -> None:
    resp = _client().post(
        "/execution-mode",
        headers={"x-api-key": "z" * 32},
        json={"mode": "manual", "operator": "alice", "operator_secret": "s" * 32},
    )
    assert resp.status_code == 401
