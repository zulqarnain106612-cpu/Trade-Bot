"""
The FSM's partial-fill machinery was never fed.

OrderFSM has carried add_partial_fill, _calculate_vwap and fill_percentage
since it was written, and nothing in src ever called them. The poll loop in
place_order_with_fsm only looked at terminal statuses, so an order that
filled in pieces reported filled_qty=0 and average_fill_price=0 for its
entire life and then jumped straight to FILLED -- the fields existed, were
documented, and were always zero.

ccxt reports `filled` and `average` cumulatively on an open order while
add_partial_fill takes an increment, so the delta is derived against what
the FSM already holds, and the increment's price is backed out of the two
cumulative VWAPs. Taking `average` as the price of the newest piece would
bias the FSM's VWAP toward the earliest fills and stop it reproducing the
exchange's own number.
"""

from __future__ import annotations

import pytest

from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
from src.execution.order_manager import OrderManager


def _filling_fsm(quantity: float = 10.0) -> OrderFSM:
    fsm = OrderFSM(
        OrderFSMState(
            order_id="o-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=quantity,
            status=OrderStatus.PENDING,
        )
    )
    fsm.transition(OrderStatus.FILLING, {})
    return fsm


def _feed(fsm: OrderFSM, filled: float, average: float) -> None:
    OrderManager()._record_incremental_fill(fsm, {"filled": filled, "average": average})


def test_a_first_increment_is_recorded() -> None:
    fsm = _filling_fsm()
    _feed(fsm, 4.0, 100.0)

    assert fsm.state.filled_qty == pytest.approx(4.0)
    assert fsm.state.average_fill_price == pytest.approx(100.0)


def test_the_fsm_vwap_reproduces_the_exchange_average() -> None:
    # 4 @ 100 then 6 more, exchange reports a cumulative average of 106.
    fsm = _filling_fsm()
    _feed(fsm, 4.0, 100.0)
    _feed(fsm, 10.0, 106.0)

    assert fsm.state.filled_qty == pytest.approx(10.0)
    assert fsm.state.average_fill_price == pytest.approx(106.0)


def test_the_increment_price_is_backed_out_not_taken_as_the_average() -> None:
    # The second piece must be priced at 110, not at the running 106.
    fsm = _filling_fsm()
    _feed(fsm, 4.0, 100.0)
    _feed(fsm, 10.0, 106.0)

    price, qty = fsm.state.filled_at_prices[-1]
    assert qty == pytest.approx(6.0)
    assert price == pytest.approx(110.0)


def test_fill_percentage_tracks_progress() -> None:
    fsm = _filling_fsm(quantity=10.0)
    _feed(fsm, 2.5, 100.0)

    assert fsm.state.fill_percentage() == pytest.approx(0.25)


def test_a_repeated_poll_with_no_progress_is_a_no_op() -> None:
    fsm = _filling_fsm()
    _feed(fsm, 4.0, 100.0)
    _feed(fsm, 4.0, 100.0)

    assert fsm.state.filled_qty == pytest.approx(4.0)
    assert len(fsm.state.filled_at_prices) == 1


def test_a_zero_fill_poll_records_nothing() -> None:
    fsm = _filling_fsm()
    _feed(fsm, 0.0, 0.0)

    assert fsm.state.filled_qty == pytest.approx(0.0)
    assert fsm.state.filled_at_prices == []


def test_missing_fields_are_skipped_not_guessed() -> None:
    fsm = _filling_fsm()
    OrderManager()._record_incremental_fill(fsm, {"filled": 4.0})
    OrderManager()._record_incremental_fill(fsm, {"average": 100.0})
    OrderManager()._record_incremental_fill(fsm, {})

    assert fsm.state.filled_qty == pytest.approx(0.0)


def test_a_non_numeric_field_does_not_raise() -> None:
    # The poll loop confirms a live order; book-keeping must never kill it.
    fsm = _filling_fsm()
    OrderManager()._record_incremental_fill(fsm, {"filled": "n/a", "average": 100.0})

    assert fsm.state.filled_qty == pytest.approx(0.0)


def test_nothing_is_recorded_outside_the_filling_state() -> None:
    fsm = OrderFSM(
        OrderFSMState(
            order_id="o-2",
            symbol="BTC/USDT",
            side="buy",
            quantity=10.0,
            status=OrderStatus.PENDING,
        )
    )
    assert fsm.state.status is OrderStatus.PENDING

    _feed(fsm, 4.0, 100.0)
    assert fsm.state.filled_qty == pytest.approx(0.0)


def test_an_overfill_is_logged_not_raised() -> None:
    # The exchange and the FSM disagreeing about the order is worth
    # surfacing, but not worth aborting the confirmation loop over.
    fsm = _filling_fsm(quantity=5.0)
    _feed(fsm, 9.0, 100.0)

    assert fsm.state.filled_qty == pytest.approx(0.0)


def test_the_terminal_transition_still_wins() -> None:
    # Partial tracking is additive; the exchange's final totals are
    # authoritative and overwrite it.
    fsm = _filling_fsm(quantity=10.0)
    _feed(fsm, 4.0, 100.0)
    fsm.transition(OrderStatus.FILLED, {"filled_qty": 10.0, "average_price": 106.0})

    assert fsm.state.filled_qty == pytest.approx(10.0)
    assert fsm.state.average_fill_price == pytest.approx(106.0)
