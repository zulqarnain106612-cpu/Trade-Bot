"""GOV-066: lifecycle transitions are validated against one table; anything
the table lacks is rejected explicitly."""

from __future__ import annotations

import pytest

from src.runtime.contracts import (
    DESIRABLE_STATES,
    OBSERVE_ONLY,
    RUNNING_STATES,
    TRANSITIONS,
    InvalidTransitionError,
    LifecycleAction,
    LifecycleState,
    UnsupportedActionError,
    next_state,
)

from ._support import COLD, HOT

A = LifecycleAction
S = LifecycleState

EXPECTED = [
    (None, A.DISCOVER, S.DISCOVERED),
    (S.DISCOVERED, A.VALIDATE, S.VALIDATED),
    (S.VALIDATED, A.INITIALIZE, S.INITIALIZED),
    (S.INITIALIZED, A.START, S.STANDBY),
    (S.STOPPED, A.START, S.STANDBY),
    (S.STANDBY, A.ACTIVATE, S.ACTIVE),
    (S.ACTIVE, A.DEACTIVATE, S.STANDBY),
    (S.ACTIVE, A.PAUSE, S.PAUSED),
    (S.PAUSED, A.RESUME, S.ACTIVE),
    (S.ACTIVE, A.RELOAD, S.ACTIVE),
    (S.PAUSED, A.DRAIN, S.DRAINED),
    (S.DRAINED, A.STOP, S.STOPPED),
    (S.FAILED, A.STOP, S.STOPPED),
    (S.QUARANTINED, A.STOP, S.STOPPED),
    (S.ACTIVE, A.REPLACE, S.ACTIVE),
    (S.STOPPED, A.REPLACE, S.INITIALIZED),
    (S.FAILED, A.ROLLBACK, S.INITIALIZED),
    (S.DISCOVERED, A.QUARANTINE, S.QUARANTINED),
]


@pytest.mark.parametrize(("state", "action", "target"), EXPECTED)
def test_table_edges(state: S | None, action: A, target: S) -> None:
    assert next_state("worker:w", state, action, HOT) is target


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (S.ACTIVE, A.STOP),  # an active component is deactivated or drained first
        (S.DISCOVERED, A.START),  # not validated, not initialised
        (S.QUARANTINED, A.QUARANTINE),
        (S.QUARANTINED, A.START),  # quarantine is left only by STOP
        (S.STOPPED, A.ACTIVATE),
        (S.STANDBY, A.PAUSE),
        (None, A.START),
        (S.ACTIVE, A.DISCOVER),
    ],
)
def test_missing_edges_are_rejected(state: S | None, action: A) -> None:
    with pytest.raises(InvalidTransitionError) as err:
        next_state("worker:w", state, action, HOT)
    assert err.value.state is state and err.value.action is action
    assert action.value in str(err.value)


def test_unregistered_state_is_named_in_the_error() -> None:
    with pytest.raises(InvalidTransitionError, match="<unregistered>"):
        next_state("worker:w", None, A.STOP, HOT)


def test_quarantine_is_reachable_from_every_other_state() -> None:
    for state in S:
        if state is not S.QUARANTINED:
            assert TRANSITIONS[(state, A.QUARANTINE)] is S.QUARANTINED


def test_every_target_of_the_table_is_a_state_and_desirable_states_are_reachable() -> None:
    targets = set(TRANSITIONS.values())
    assert DESIRABLE_STATES <= targets
    assert RUNNING_STATES <= DESIRABLE_STATES


def test_discover_needs_no_capability() -> None:
    assert next_state("worker:w", None, A.DISCOVER, OBSERVE_ONLY) is S.DISCOVERED


def test_cold_swap_needs_no_hot_swap_capability() -> None:
    assert next_state("worker:w", S.DRAINED, A.REPLACE, COLD) is S.INITIALIZED
    with pytest.raises(UnsupportedActionError, match="no hot swap"):
        next_state("worker:w", S.ACTIVE, A.REPLACE, COLD)
