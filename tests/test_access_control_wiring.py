"""
Wiring tests for role-based access control.

access_control.py shipped with a full permission table and no caller: every
authenticated key had every authority. These cover the key-to-role mapping
in auth.py and the 403 dependency in main.py, with the emphasis on the
invariant that matters — configuring the optional read-only key must not
change what the existing key can do.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.access_control import Permission, Role
from src.api.auth import verify_api_key


PRIMARY = "a" * 40
READONLY = "b" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real API_READONLY_KEY must not leak into these tests."""
    monkeypatch.setenv("API_SECRET_KEY", PRIMARY)
    monkeypatch.delenv("API_READONLY_KEY", raising=False)


class TestRoleResolution:
    def test_primary_key_is_trade_authorizing(self) -> None:
        assert verify_api_key(PRIMARY) is Role.TRADE_AUTHORIZING

    def test_single_key_deployment_is_unchanged(self) -> None:
        """No read-only key configured => nobody is downgraded."""
        assert verify_api_key(PRIMARY) is Role.TRADE_AUTHORIZING
        with pytest.raises(HTTPException) as exc:
            verify_api_key(READONLY)
        assert exc.value.status_code == 401

    def test_readonly_key_resolves_to_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_READONLY_KEY", READONLY)
        assert verify_api_key(READONLY) is Role.READ_ONLY

    def test_configuring_readonly_key_does_not_downgrade_primary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding a key may only ever remove authority from the new key."""
        monkeypatch.setenv("API_READONLY_KEY", READONLY)
        assert verify_api_key(PRIMARY) is Role.TRADE_AUTHORIZING

    def test_missing_key_still_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            verify_api_key(None)
        assert exc.value.status_code == 401

    def test_short_readonly_key_is_503_not_a_silent_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_READONLY_KEY", "tooshort")
        with pytest.raises(HTTPException) as exc:
            verify_api_key(PRIMARY)
        assert exc.value.status_code == 503

    def test_readonly_key_equal_to_primary_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identical keys make the two roles indistinguishable — fail loudly."""
        monkeypatch.setenv("API_READONLY_KEY", PRIMARY)
        with pytest.raises(HTTPException) as exc:
            verify_api_key(PRIMARY)
        assert exc.value.status_code == 503


class TestRequiresDependency:
    def _dep(self, permission: Permission):
        from src.api.main import requires

        return requires(permission)

    def test_trade_authorizing_key_passes_every_permission(self) -> None:
        for permission in Permission:
            assert self._dep(permission)(PRIMARY) is None

    def test_read_only_key_is_denied_with_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_READONLY_KEY", READONLY)
        with pytest.raises(HTTPException) as exc:
            self._dep(Permission.APPROVE_TRADE)(READONLY)
        # 403 not 401: the caller authenticated, it just lacks the authority.
        assert exc.value.status_code == 403

    def test_read_only_key_may_still_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_READONLY_KEY", READONLY)
        assert self._dep(Permission.VIEW_TRADES)(READONLY) is None

    def test_read_only_key_cannot_change_execution_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_READONLY_KEY", READONLY)
        with pytest.raises(HTTPException) as exc:
            self._dep(Permission.CHANGE_EXECUTION_MODE)(READONLY)
        assert exc.value.status_code == 403

    def test_invalid_key_is_401_before_any_permission_check(self) -> None:
        with pytest.raises(HTTPException) as exc:
            self._dep(Permission.APPROVE_TRADE)("z" * 40)
        assert exc.value.status_code == 401


class TestGatedEndpointsDeclareTheDependency:
    """The gates are only worth having if they are actually attached."""

    _MUTATING = {
        "/approvals/{request_id}/resolve",
        "/execution-mode",
        "/risk-controls",
        "/self-tuning/pause",
        "/self-tuning/resume",
        "/self-tuning/rollback/{param_name}",
    }

    def test_every_mutating_post_route_is_permission_gated(self) -> None:
        from fastapi.routing import APIRoute

        from src.api.main import app

        def _is_gated(route: APIRoute) -> bool:
            return any(
                getattr(dep.call, "__name__", "") == "_dependency"
                for dep in route.dependant.dependencies
            )

        gated = {
            route.path
            for route in app.routes
            if isinstance(route, APIRoute) and "POST" in route.methods and _is_gated(route)
        }
        assert gated >= self._MUTATING, f"ungated: {self._MUTATING - gated}"
