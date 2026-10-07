"""GOV-066: capabilities are explicit; an undeclared action is never performed
by bookkeeping."""

from __future__ import annotations

import pytest

from src.runtime.contracts import (
    OBSERVE_ONLY,
    CapabilitySet,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    UnsupportedActionError,
    next_state,
)

A = LifecycleAction
S = LifecycleState


def test_observe_only_declares_nothing() -> None:
    assert OBSERVE_ONLY.actions == frozenset()
    assert not OBSERVE_ONLY.supports_pause and not OBSERVE_ONLY.supports_reload
    assert OBSERVE_ONLY.to_dict() == {
        "actions": [],
        "supports_reload": False,
        "supports_pause": False,
        "supports_hot_swap": False,
        "requires_drain_for_replace": True,
    }


def test_derived_flags_follow_the_declared_actions() -> None:
    caps = CapabilitySet(frozenset({A.PAUSE, A.RESUME, A.RELOAD}))
    assert caps.supports_pause and caps.supports_reload
    assert caps.to_dict()["actions"] == ["PAUSE", "RELOAD", "RESUME"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"actions": {A.STOP}}, "frozenset"),
        ({"actions": frozenset({"STOP"})}, "frozenset"),
        ({"actions": frozenset({A.DISCOVER})}, "bookkeeping"),
        ({"actions": frozenset({A.PAUSE})}, "together"),
        ({"actions": frozenset({A.RESUME})}, "together"),
        (
            {"actions": frozenset({A.REPLACE}), "supports_hot_swap": True},
            "mutually exclusive",
        ),
        (
            {
                "actions": frozenset({A.STOP}),
                "supports_hot_swap": True,
                "requires_drain_for_replace": False,
            },
            "needs REPLACE or ROLLBACK",
        ),
    ],
)
def test_inconsistent_capability_sets_are_rejected(kwargs: dict, match: str) -> None:
    with pytest.raises(RuntimeContractError, match=match):
        CapabilitySet(**kwargs)


def test_an_undeclared_action_is_unsupported_even_when_the_table_allows_it() -> None:
    caps = CapabilitySet(frozenset({A.ACTIVATE}))
    assert next_state("engine:E-01", S.STANDBY, A.ACTIVATE, caps) is S.ACTIVE
    with pytest.raises(UnsupportedActionError, match="not declared") as err:
        next_state("engine:E-01", S.ACTIVE, A.PAUSE, caps)
    assert err.value.component_id == "engine:E-01" and err.value.action is A.PAUSE


def test_hot_swap_capability_is_required_for_a_running_swap() -> None:
    hot = CapabilitySet(
        frozenset({A.ROLLBACK}), supports_hot_swap=True, requires_drain_for_replace=False
    )
    assert next_state("model:m", S.ACTIVE, A.ROLLBACK, hot) is S.ACTIVE
