"""
Role-based access control primitives — v8 Institutional-Grade Operations.

Defines the read-only vs. trade-authorizing role split and a pure
permission-check function. Still decoupled from key storage/verification:
src/api/auth.py owns the key-to-role mapping, this module owns only the
role-to-permission table.

The mapping convention is additive and opt-in: API_SECRET_KEY continues to
authenticate as TRADE_AUTHORIZING, and the optional API_READONLY_KEY
authenticates as READ_ONLY. A deployment that sets only the first is
unaffected, so introducing this table cannot weaken an existing install —
the only reachable change is that a newly-configured second key has *less*
authority than the key that already exists.

src/api/main.py's requires() dependency turns a missing permission into
HTTP 403 on the mutating endpoints (approvals, execution mode, risk
controls, self-tuning pause/resume/rollback).

Authority:
  - Domain Prior: no hidden failures or skipped validation — a role check
    must fail closed (deny) on any unrecognized role/permission pair
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Final


class Role(Enum):
    READ_ONLY = "read_only"
    TRADE_AUTHORIZING = "trade_authorizing"


class Permission(Enum):
    VIEW_STATUS = "view_status"
    VIEW_TRADES = "view_trades"
    APPROVE_TRADE = "approve_trade"
    CHANGE_EXECUTION_MODE = "change_execution_mode"


# MappingProxyType, not a bare dict: this is the authorization table. The
# values are already frozensets, so only the outer mapping was writable —
# and a single write anywhere in the process could grant a role a permission
# it was never configured with, for every request thereafter.
_ROLE_PERMISSIONS: Final[Mapping[Role, frozenset[Permission]]] = MappingProxyType(
    {
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
)


def role_has_permission(role: Role, permission: Permission) -> bool:
    """Fails closed: an unrecognized role has zero permissions."""
    return permission in _ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(role: Role, permission: Permission) -> None:
    """Raises PermissionError if role lacks permission; no-op otherwise."""
    if not role_has_permission(role, permission):
        raise PermissionError(f"role {role.value!r} lacks permission {permission.value!r}")
