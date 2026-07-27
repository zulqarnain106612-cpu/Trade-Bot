"""Tests for the v8 role-based access control primitives."""

from __future__ import annotations

import pytest

from src.api.access_control import Permission, Role, require_permission, role_has_permission


def test_read_only_can_view_status() -> None:
    assert role_has_permission(Role.READ_ONLY, Permission.VIEW_STATUS)


def test_read_only_cannot_approve_trade() -> None:
    assert not role_has_permission(Role.READ_ONLY, Permission.APPROVE_TRADE)


def test_trade_authorizing_can_approve_trade() -> None:
    assert role_has_permission(Role.TRADE_AUTHORIZING, Permission.APPROVE_TRADE)


def test_trade_authorizing_can_also_view() -> None:
    assert role_has_permission(Role.TRADE_AUTHORIZING, Permission.VIEW_STATUS)
    assert role_has_permission(Role.TRADE_AUTHORIZING, Permission.VIEW_TRADES)


def test_require_permission_raises_on_denial() -> None:
    with pytest.raises(PermissionError, match="lacks permission"):
        require_permission(Role.READ_ONLY, Permission.CHANGE_EXECUTION_MODE)


def test_require_permission_noop_on_grant() -> None:
    require_permission(Role.TRADE_AUTHORIZING, Permission.CHANGE_EXECUTION_MODE)
