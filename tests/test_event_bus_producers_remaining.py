"""
INV-032 -- the producers that had no push path at all.

Each of these reaches the operator today only through a poll, and each is a
thing they would want to know the instant it happens:

  capital_floor  the outermost backstop. It halts ALL trading and never
                 auto-clears -- someone has to come and re-authorize it. A
                 halted system that looks quiet is the worst possible
                 rendering of that state.
  killswitch     capital has already been pulled from a strategy by the time
                 anyone is told.
  order          the FSM is the only place that knows a fill happened; the
                 trades table only learns once the position closes.
"""

from __future__ import annotations

import pytest

from src.eventbus import EventBus
from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
from src.risk.capital_preservation_floor import CapitalPreservationFloor


@pytest.fixture
def floor_bus(monkeypatch) -> EventBus:
    bus = EventBus()
    monkeypatch.setattr("src.risk.capital_preservation_floor.get_event_bus", lambda: bus)
    return bus


@pytest.fixture
def fsm_bus(monkeypatch) -> EventBus:
    bus = EventBus()
    monkeypatch.setattr("src.execution.order_fsm.get_event_bus", lambda: bus)
    return bus


class TestCapitalFloor:
    def test_tripping_the_floor_publishes(self, floor_bus: EventBus) -> None:
        sub = floor_bus.subscribe(["capital_floor"])
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)

        floor.update_equity(1000.0)
        assert floor.update_equity(600.0) is False  # 40% drawdown

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["halted"] is True
        assert data["drawdown_pct"] == pytest.approx(0.4)

    def test_a_normal_mark_publishes_nothing(self, floor_bus: EventBus) -> None:
        sub = floor_bus.subscribe(["capital_floor"])
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)

        floor.update_equity(1000.0)
        floor.update_equity(950.0)

        assert len(sub._queue) == 0

    def test_it_publishes_once_not_on_every_later_mark(self, floor_bus: EventBus) -> None:
        """
        update_equity returns False forever once halted. Publishing on each
        of those would bury every other event in the buffer.
        """
        sub = floor_bus.subscribe(["capital_floor"])
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)

        floor.update_equity(1000.0)
        floor.update_equity(600.0)
        floor.update_equity(590.0)
        floor.update_equity(580.0)

        assert len(sub._queue) == 1

    def test_re_authorization_publishes_the_clear(self, floor_bus: EventBus) -> None:
        sub = floor_bus.subscribe(["capital_floor"])
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(1000.0)
        floor.update_equity(600.0)

        floor.re_authorize(authorized_by="alice", reason="reviewed", at_ms=1)

        assert sub._queue[-1].data == {
            "halted": False,
            "authorized_by": "alice",
            "reason": "reviewed",
            "at_ms": 1,
        }

    def test_a_corrupt_mark_still_raises(self, floor_bus: EventBus) -> None:
        """
        The NaN/inf guard is the difference between a one-tick fault and a
        permanently disabled backstop. Adding a publish must not soften it.
        """
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)

        with pytest.raises(ValueError):
            floor.update_equity(float("nan"))


class TestOrderFSM:
    def _state(self) -> OrderFSMState:
        return OrderFSMState(
            order_id="o1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )

    def test_a_transition_publishes_both_ends(self, fsm_bus: EventBus) -> None:
        sub = fsm_bus.subscribe(["order"])
        fsm = OrderFSM(self._state())

        fsm.transition(OrderStatus.FILLING)

        data = sub._queue[0].data
        assert data["from"] == "pending"
        assert data["to"] == "filling"
        assert data["order_id"] == "o1"
        assert data["terminal"] is False

    def test_a_terminal_transition_is_marked(self, fsm_bus: EventBus) -> None:
        sub = fsm_bus.subscribe(["order"])
        fsm = OrderFSM(self._state())

        fsm.transition(OrderStatus.FILLING)
        fsm.transition(OrderStatus.FILLED, {"filled_qty": 1.0, "average_price": 100.0})

        assert sub._queue[-1].data["to"] == "filled"
        assert sub._queue[-1].data["terminal"] is True

    def test_a_rejected_transition_publishes_nothing(self, fsm_bus: EventBus) -> None:
        """
        The publish sits after the guards, so an invalid move cannot announce
        a state the FSM refused to enter.
        """
        from src.execution.order_fsm import OrderFSMError

        sub = fsm_bus.subscribe(["order"])
        fsm = OrderFSM(self._state())

        with pytest.raises(OrderFSMError):
            fsm.transition(OrderStatus.FILLED)  # PENDING -> FILLED is invalid

        assert len(sub._queue) == 0

    def test_the_state_is_committed_before_the_event(self, fsm_bus: EventBus) -> None:
        """
        A subscriber that saw FILLING while the FSM still read PENDING would
        be reporting a state the system had not reached.
        """
        observed: list[str] = []
        fsm = OrderFSM(self._state())

        sub = fsm_bus.subscribe(["order"])
        original_offer = sub._offer

        def _capture(event):
            observed.append(fsm.state.status.value)
            original_offer(event)

        sub._offer = _capture
        fsm.transition(OrderStatus.FILLING)

        assert observed == ["filling"]
