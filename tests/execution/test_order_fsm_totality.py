"""
EXEC-002, EXEC-004, INV-003 — the order state machine is total and its
transitions are legal.

"Total" is the word doing the work. It is not enough that the legal
transitions behave correctly; every `(state, event)` pair must have a defined
outcome, because the one that does not is where an order ends up in a state
nothing knows how to close.

The suite therefore enumerates the **entire** cross product of states and
target states and asserts each cell is either a permitted transition or a
refusal with a reason — never a silent no-op, and never an unhandled
exception type the caller was not told about.

The invariant that matters most is the terminal one: a terminal state is
immutable, so an order that has been recorded as FILLED cannot later be
recorded as anything else, and an order that failed cannot be quietly
resurrected into a fill.
"""

from __future__ import annotations

import itertools

import pytest

from src.execution.order_fsm import (
    OrderFSM,
    OrderFSMError,
    OrderFSMState,
    OrderStatus,
)

TERMINAL = (
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.TIMEOUT,
    OrderStatus.FAILED,
)
ACTIVE = (OrderStatus.PENDING, OrderStatus.FILLING)

#: The declared table, restated here on purpose. A test that reads the
#: implementation's own table cannot detect a change to it; this is the
#: independent statement the implementation is compared against.
EXPECTED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.FILLING, OrderStatus.TIMEOUT, OrderStatus.FAILED}),
    OrderStatus.FILLING: frozenset(
        {
            OrderStatus.FILLING,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.TIMEOUT,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.TIMEOUT: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


def fsm(status: OrderStatus = OrderStatus.PENDING, quantity: float = 1.0) -> OrderFSM:
    return OrderFSM(
        OrderFSMState(
            order_id="ord-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=quantity,
            status=status,
            idempotency_key="key-1",
        )
    )


class TestTheTableIsTotal:
    @pytest.mark.parametrize(("start", "target"), list(itertools.product(OrderStatus, OrderStatus)))
    def test_every_state_pair_has_a_defined_outcome(self, start, target):
        machine = fsm(start)
        permitted = target in EXPECTED_TRANSITIONS[start]
        if permitted:
            machine.transition(target, {"filled_qty": 1.0, "average_price": 100.0})
            assert machine.state.status is target
        else:
            with pytest.raises(OrderFSMError):
                machine.transition(target, {"filled_qty": 1.0, "average_price": 100.0})
            # A refusal must leave the state alone. A partially-applied
            # transition is worse than a rejected one.
            assert machine.state.status is start

    def test_the_implementation_table_matches_the_declared_one(self):
        assert dict(OrderFSM._VALID_TRANSITIONS) == EXPECTED_TRANSITIONS

    def test_every_status_appears_in_the_table(self):
        assert set(OrderFSM._VALID_TRANSITIONS) == set(OrderStatus)

    def test_the_table_cannot_be_edited_at_runtime(self):
        # The values are frozensets and the outer mapping is a MappingProxy,
        # so a caller holding a reference cannot widen the transition rules
        # for every order in the process.
        with pytest.raises(TypeError):
            OrderFSM._VALID_TRANSITIONS[OrderStatus.FILLED] = frozenset({OrderStatus.FILLING})


class TestTerminalStatesAreImmutable:
    @pytest.mark.parametrize("terminal", TERMINAL)
    @pytest.mark.parametrize("target", list(OrderStatus))
    def test_nothing_moves_out_of_a_terminal_state(self, terminal, target):
        machine = fsm(terminal)
        with pytest.raises(OrderFSMError, match="terminal state"):
            machine.transition(target)
        assert machine.state.status is terminal

    @pytest.mark.parametrize("terminal", TERMINAL)
    def test_a_terminal_order_reports_itself_terminal(self, terminal):
        assert fsm(terminal).state.is_terminal()
        assert not fsm(terminal).state.is_active()

    @pytest.mark.parametrize("active", ACTIVE)
    def test_an_active_order_reports_itself_active(self, active):
        assert fsm(active).state.is_active()
        assert not fsm(active).state.is_terminal()

    def test_terminal_and_active_partition_the_states(self):
        # No status is both, and none is neither -- otherwise a caller
        # branching on the two would have a hole.
        for status in OrderStatus:
            state = fsm(status).state
            assert state.is_terminal() != state.is_active()


class TestAFailedOrderCannotBecomeAFill:
    """INV-003, at the state-machine level."""

    @pytest.mark.parametrize("terminal", TERMINAL)
    def test_no_terminal_state_can_reach_filled(self, terminal):
        machine = fsm(terminal)
        with pytest.raises(OrderFSMError):
            machine.transition(OrderStatus.FILLED, {"filled_qty": 1.0, "average_price": 100.0})
        assert machine.state.status is terminal

    def test_pending_cannot_jump_straight_to_filled(self):
        # The FILLING step is where the exchange confirmation is recorded. A
        # jump past it would book a fill for an order the venue never
        # acknowledged.
        machine = fsm(OrderStatus.PENDING)
        with pytest.raises(OrderFSMError, match="Invalid transition"):
            machine.transition(OrderStatus.FILLED, {"filled_qty": 1.0, "average_price": 100.0})

    def test_pending_cannot_be_cancelled_directly(self):
        with pytest.raises(OrderFSMError, match="Invalid transition"):
            fsm(OrderStatus.PENDING).transition(OrderStatus.CANCELLED)


class TestPartialFills:
    def test_fills_aggregate(self):
        machine = fsm(OrderStatus.FILLING, quantity=1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        assert machine.state.filled_qty == pytest.approx(1.0)

    def test_the_average_is_volume_weighted_not_arithmetic(self):
        # (0.4*100 + 0.6*110) / 1.0 = 106, not (100+110)/2 = 105.
        machine = fsm(OrderStatus.FILLING, quantity=1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        assert machine.state.average_fill_price == pytest.approx(106.0)

    def test_overfilling_is_refused_and_leaves_the_quantity_alone(self):
        machine = fsm(OrderStatus.FILLING, quantity=1.0)
        machine.add_partial_fill(0.9, 100.0)
        with pytest.raises(OrderFSMError, match="exceed order quantity"):
            machine.add_partial_fill(0.2, 100.0)
        assert machine.state.filled_qty == pytest.approx(0.9)

    @pytest.mark.parametrize("qty", [0.0, -0.1])
    def test_a_non_positive_fill_quantity_is_refused(self, qty):
        with pytest.raises(OrderFSMError, match="qty must be > 0"):
            fsm(OrderStatus.FILLING).add_partial_fill(qty, 100.0)

    @pytest.mark.parametrize("price", [0.0, -1.0])
    def test_a_non_positive_fill_price_is_refused(self, price):
        with pytest.raises(OrderFSMError, match="price must be > 0"):
            fsm(OrderStatus.FILLING).add_partial_fill(0.1, price)

    @pytest.mark.parametrize("status", [s for s in OrderStatus if s is not OrderStatus.FILLING])
    def test_a_fill_outside_the_filling_state_is_refused(self, status):
        with pytest.raises(OrderFSMError, match="Cannot add partial fill"):
            fsm(status).add_partial_fill(0.1, 100.0)

    def test_an_order_filled_in_many_pieces_can_actually_complete(self):
        # Found by writing this suite. 200 fills of 0.001 sum to
        # 0.19900000000000015 in float64, which is greater than 0.2 by
        # 1.5e-16 -- so an exact overfill comparison rejected the *last*
        # fill of an order that had filled perfectly, leaving it stuck in
        # FILLING while the venue believed it complete.
        machine = fsm(OrderStatus.FILLING, quantity=0.2)
        for _ in range(200):
            machine.add_partial_fill(0.001, 100.0)
        assert machine.state.filled_qty == pytest.approx(0.2, rel=1e-12)
        assert machine.state.fill_percentage() == pytest.approx(1.0, rel=1e-12)

    def test_the_recorded_quantity_never_exceeds_the_order(self):
        # The drift is clamped away rather than carried, so a downstream
        # consumer never sees a fill one ulp larger than the order.
        machine = fsm(OrderStatus.FILLING, quantity=0.2)
        for _ in range(200):
            machine.add_partial_fill(0.001, 100.0)
        assert machine.state.filled_qty <= 0.2

    def test_a_real_overfill_is_still_refused(self):
        # The tolerance is 1e-9 relative, so anything a venue could actually
        # report as an overfill still fails.
        machine = fsm(OrderStatus.FILLING, quantity=1.0)
        machine.add_partial_fill(0.999999, 100.0)
        with pytest.raises(OrderFSMError, match="exceed order quantity"):
            machine.add_partial_fill(0.01, 100.0)

    def test_the_fill_percentage_tracks_the_aggregate(self):
        machine = fsm(OrderStatus.FILLING, quantity=2.0)
        machine.add_partial_fill(0.5, 100.0)
        assert machine.state.fill_percentage() == pytest.approx(0.25)

    def test_a_zero_quantity_order_reports_zero_rather_than_dividing(self):
        assert fsm(OrderStatus.FILLING, quantity=0.0).state.fill_percentage() == 0.0


class TestTheStateIsSerialisable:
    def test_every_field_survives_to_dict(self):
        # The state is the thing a recovery pass reads back. A field that
        # does not serialise is a field that is lost on restart.
        machine = fsm(OrderStatus.FILLING, quantity=1.0)
        machine.add_partial_fill(0.5, 100.0)
        as_dict = machine.state.to_dict()
        for key in (
            "order_id",
            "symbol",
            "side",
            "quantity",
            "status",
            "filled_qty",
            "filled_at_prices",
            "average_fill_price",
            "idempotency_key",
        ):
            assert key in as_dict

    def test_the_idempotency_key_is_persisted(self):
        # So a recovery pass can match an order back to its originating
        # intent instead of guessing from symbol/side/quantity -- and can
        # tell "already submitted" from "never sent".
        assert fsm().state.to_dict()["idempotency_key"] == "key-1"

    def test_the_status_serialises_as_its_value_not_its_repr(self):
        assert fsm(OrderStatus.FILLING).state.to_dict()["status"] == "filling"
