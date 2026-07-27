"""
Role-based access control primitives — v8 Institutional-Grade Operations.

Defines the read-only vs. trade-authorizing role split and a pure
permission-check function. Deliberately decoupled from key storage/
verification (src/api/auth.py) — wiring this into live request
dependencies requires deciding a new API-key-to-role mapping convention
(e.g. a second env var for a read-only key), which touches the existing
single-key auth model and env configuration. That's a security-sensitive
config decision left for explicit follow-up rather than made unilaterally
here; this module provides the tested role logic that follow-up will
consume.

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
