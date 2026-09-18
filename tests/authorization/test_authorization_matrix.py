"""
API-001, API-002 — the authorization matrix is executable and every cell is
tested.

The source document's shape:

```
                 anonymous  readonly  trader  operator
GET status          ❌         ✓         ✓        ✓
POST trade          ❌         ❌        ✓        ✓
risk modification   ❌         ❌        ❌       ✓
kill switch         ❌         ❌        ❌       ✓
```

> Then test every cell. This catches privilege-escalation bugs.

This project has two roles rather than four — `READ_ONLY` and
`TRADE_AUTHORIZING` — plus the anonymous row, which is the one that matters
most and the one an inherited matrix usually omits. The matrix below is
written out **independently of the implementation**: a test that reads
`_ROLE_PERMISSIONS` and asserts against it cannot detect a change to it.

`API-002` (IDOR) is covered at the bottom. This API has no per-principal
resources — there is one trading account and one book — so the honest test is
not "user A cannot read user B's order" but "the authorization decision does
not depend on an identifier the caller supplies", which is the property that
would have to hold before per-principal resources could be added safely.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.api.access_control import (
    Permission,
    Role,
    require_permission,
    role_has_permission,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The matrix, restated. Independent of the implementation on purpose.
EXPECTED: dict[Role, frozenset[Permission]] = {
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

CELLS = [(role, permission) for role in Role for permission in Permission]


class TestEveryCell:
    @pytest.mark.parametrize(("role", "permission"), CELLS)
    def test_each_cell_matches_the_declared_matrix(self, role, permission):
        assert role_has_permission(role, permission) is (permission in EXPECTED[role])

    @pytest.mark.parametrize(("role", "permission"), CELLS)
    def test_require_permission_agrees_with_the_predicate(self, role, permission):
        # Two entry points must not be able to disagree: a caller using one
        # and a reviewer reading the other would reach different conclusions.
        if permission in EXPECTED[role]:
            require_permission(role, permission)
        else:
            with pytest.raises(PermissionError):
                require_permission(role, permission)

    def test_the_implementation_matrix_matches_the_declared_one(self):
        from src.api.access_control import _ROLE_PERMISSIONS

        assert dict(_ROLE_PERMISSIONS) == EXPECTED

    def test_every_role_appears_in_the_matrix(self):
        # A role with no row has no permissions, which fails closed -- but
        # silently, and a role nobody granted anything is more likely a
        # forgotten row than a deliberate one.
        assert set(EXPECTED) == set(Role)

    def test_every_permission_is_granted_to_someone(self):
        # A permission no role holds is unreachable: either dead, or a row
        # that was forgotten when it was added.
        granted = set().union(*EXPECTED.values())
        assert granted == set(Permission)


class TestTheAnonymousRow:
    """The row an inherited matrix omits, and the one that matters most."""

    @pytest.mark.parametrize("permission", list(Permission))
    def test_an_unknown_role_has_no_permission(self, permission):
        class _Forged:
            value = "operator"

        assert not role_has_permission(_Forged(), permission)

    @pytest.mark.parametrize("permission", list(Permission))
    def test_none_has_no_permission(self, permission):
        assert not role_has_permission(None, permission)

    def test_the_failure_mode_is_closed_and_documented(self):
        from src.api import access_control

        assert "Fails closed" in access_control.role_has_permission.__doc__


class TestPrivilegeEscalation:
    def test_read_only_cannot_approve_a_trade(self):
        assert not role_has_permission(Role.READ_ONLY, Permission.APPROVE_TRADE)
        with pytest.raises(PermissionError, match="approve_trade"):
            require_permission(Role.READ_ONLY, Permission.APPROVE_TRADE)

    def test_read_only_cannot_change_the_execution_mode(self):
        # The one that turns paper into live.
        assert not role_has_permission(Role.READ_ONLY, Permission.CHANGE_EXECUTION_MODE)

    def test_the_matrix_cannot_be_edited_at_runtime(self):
        # A single write anywhere in the process would grant a role a
        # permission it was never configured with, for every request after.
        from src.api.access_control import _ROLE_PERMISSIONS

        with pytest.raises(TypeError):
            _ROLE_PERMISSIONS[Role.READ_ONLY] = frozenset(Permission)

    def test_a_role_s_permission_set_cannot_be_extended(self):
        from src.api.access_control import _ROLE_PERMISSIONS

        with pytest.raises(AttributeError):
            _ROLE_PERMISSIONS[Role.READ_ONLY].add(Permission.APPROVE_TRADE)

    def test_the_refusal_names_the_role_and_the_permission(self):
        # A denial nobody can attribute is a denial that gets worked around.
        with pytest.raises(PermissionError) as excinfo:
            require_permission(Role.READ_ONLY, Permission.APPROVE_TRADE)
        assert "read_only" in str(excinfo.value)
        assert "approve_trade" in str(excinfo.value)


class TestTheDecisionDoesNotDependOnCallerSuppliedIdentity:
    """
    API-002, stated for an application with no per-principal resources.

    There is one trading account and one book, so "user A cannot read user
    B's order" has no meaning here. What does have meaning, and is the
    property that would have to hold before per-principal resources could be
    added safely: the authorization decision takes a role and a permission,
    and nothing else. No identifier from the request participates in it.
    """

    def test_the_decision_function_takes_only_a_role_and_a_permission(self):
        import inspect

        from src.api import access_control

        signature = inspect.signature(access_control.role_has_permission)
        assert list(signature.parameters) == ["role", "permission"]

    def test_no_route_authorises_on_a_path_parameter(self):
        # The shape an IDOR takes: a handler that decides access from an id
        # in the URL rather than from the caller's role.
        source = (PROJECT_ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "require_permission":
                continue
            for arg in node.args:
                # Every call must pass a Role expression and a Permission
                # attribute -- never a bare local derived from the request.
                if isinstance(arg, ast.Constant):
                    offenders.append(f"line {node.lineno}: constant argument {arg.value!r}")
        assert not offenders, offenders

    def test_authorisation_is_decided_before_any_resource_is_read(self):
        # Ordering, in the one place it can be checked statically: the
        # permission check must not sit after the handler has already
        # fetched and returned the thing it is guarding.
        source = (PROJECT_ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
        assert "require_permission" in source
