"""
The order transition table must be unwritable, not merely unrebindable.

_VALID_TRANSITIONS decides whether an order may legally move between
states. Final stops rebinding the name; it does nothing about mutating the
dict behind it, so any caller could have legalised an illegal transition
for every OrderFSM in the process — including a terminal state gaining an
exit.
"""

from __future__ import annotations

import pytest

from src.execution.order_fsm import OrderFSM, OrderStatus


def test_the_table_rejects_new_transitions() -> None:
    with pytest.raises(TypeError):
        OrderFSM._VALID_TRANSITIONS[OrderStatus.FILLED] = frozenset({OrderStatus.FILLING})


def test_the_table_rejects_deletion() -> None:
    with pytest.raises(TypeError):
        del OrderFSM._VALID_TRANSITIONS[OrderStatus.PENDING]


def test_terminal_states_stay_terminal() -> None:
    for terminal in (
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.TIMEOUT,
        OrderStatus.FAILED,
    ):
        assert OrderFSM._VALID_TRANSITIONS[terminal] == frozenset()


def test_the_table_is_still_readable() -> None:
    # Immutability must not cost the lookup the FSM actually performs.
    assert OrderStatus.FILLING in OrderFSM._VALID_TRANSITIONS[OrderStatus.PENDING]
    assert OrderFSM._VALID_TRANSITIONS.get(OrderStatus.FILLED) == frozenset()
