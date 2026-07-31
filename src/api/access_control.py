"""
Role-based access control primitives — v8 Institutional-Grade Operations.

Defines the read-only vs. trade-authorizing role split and a pure
permission-check function. Key storage/verification stays in
src/api/auth.py, which maps a presented key to a Role here:
API_SECRET_KEY → TRADE_AUTHORIZING, the optional API_READONLY_KEY →
READ_ONLY. src/api/main.py's `requires(permission)` dependency turns a
denial into HTTP 403 on the mutating routes. Leaving API_READONLY_KEY
unset keeps the original single-key model, in which every authenticated
caller is trade-authorizing.

Authority:
  - Domain Prior: no hidden failures or skipped validation — a role check
    must fail closed (deny) on any unrecognized role/permission pair
"""

from __future__ import annotations

from enum import Enum


class Role(Enum):
    READ_ONLY = "read_only"
    TRADE_AUTHORIZING = "trade_authorizing"


class Permission(Enum):
    VIEW_STATUS = "view_status"
    VIEW_TRADES = "view_trades"
    APPROVE_TRADE = "approve_trade"
    CHANGE_EXECUTION_MODE = "change_execution_mode"


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.READ_ONLY: frozenset({Permission.VIEW_STATUS, Permission.VIEW_TRADES}),
    Role.TRADE_AUTHORIZING: frozenset(
        {
            Permission.VIEW_STATUS,
            Permission.VIEW_TRADES,
            Permission.APPROVE_TRADE,
            Permission.CHANGE_EXECUTION_MODE,
        }
    ),
}


def role_has_permission(role: Role, permission: Permission) -> bool:
    """Fails closed: an unrecognized role has zero permissions."""
    return permission in _ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(role: Role, permission: Permission) -> None:
    """Raises PermissionError if role lacks permission; no-op otherwise."""
    if not role_has_permission(role, permission):
        raise PermissionError(f"role {role.value!r} lacks permission {permission.value!r}")
