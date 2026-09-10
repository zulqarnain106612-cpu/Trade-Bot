"""A destructive tool must never be reachable from a PTC script.

CLAUDE.md states the rule directly:

    Never mark a destructive, rate-limited, or side-effecting tool
    orchestratable=True in tools/registry.py. Those stay direct-call-only.

Nothing enforced it. The flag is a keyword argument on a decorator, and the
consequence of getting it wrong is not a failing test somewhere -- it is a
generated script importing the tool from registry.namespace() and calling it
in a loop, which for something like delete_record means the damage is done
before anyone reads the script.

The check is name-based, and deliberately so: a script author choosing a
verb is the earliest point at which intent is visible, and it is the only
signal available without executing the tool. It is a floor, not a proof --
a side-effecting tool named `fetch_summary` still passes. Its job is to
catch the obvious mistake at review time rather than in production.

Import side effects are the point here: importing tools.example_tools is
what populates the registry, so this exercises the real registration path
rather than a fixture.
"""

from __future__ import annotations

import importlib

import pytest

from tools.registry import registry

# Imported for the registration side effect, not for a name. import_module
# rather than a plain import with a suppression comment: that silenced ruff
# but not CodeQL, which still read an unused binding and flagged it. This
# form has no binding to be unused, and says why the import is here.
importlib.import_module("tools.example_tools")

# Verbs that change something, cost money, or cannot be undone. Matched
# against the tool name's leading token only, not anywhere in the name, so
# `get_deleted_count` reads as `get` and does not trip on `delete`. The cost
# of matching only the head is that `sync_and_delete_all` reads as `sync` and
# passes -- consistent with this being a floor rather than a proof.
SIDE_EFFECTING_VERBS = frozenset(
    {
        "cancel",
        "close",
        "create",
        "delete",
        "deploy",
        "drop",
        "execute",
        "flatten",
        "liquidate",
        "modify",
        "place",
        "post",
        "purge",
        "remove",
        "reset",
        "send",
        "set",
        "submit",
        "transfer",
        "truncate",
        "update",
        "withdraw",
        "write",
    }
)


def _leading_verb(tool_name: str) -> str:
    return tool_name.split("_", 1)[0].lower()


def test_no_orchestratable_tool_is_named_for_a_side_effect() -> None:
    offenders = sorted(
        name
        for name in registry.list_orchestratable()
        if _leading_verb(name) in SIDE_EFFECTING_VERBS
    )

    assert not offenders, (
        "These tools are marked orchestratable=True but are named for an "
        "action that changes something: " + ", ".join(offenders) + ". "
        "orchestratable=True means a generated script may import the tool "
        "and call it in a loop without a human in the conversation. See "
        "CLAUDE.md. If the name is misleading rather than the flag, rename "
        "the tool."
    )


def test_the_known_destructive_tools_are_direct_call_only() -> None:
    """Guards the check above against passing because nothing is registered."""
    direct_only = registry.list_direct_only()

    assert "delete_record" in direct_only
    assert "send_email" in direct_only


def test_assert_orchestratable_rejects_a_direct_only_tool() -> None:
    """The runtime half of the same rule, at the point a script would call it."""
    with pytest.raises(PermissionError):
        registry.assert_orchestratable("delete_record")
