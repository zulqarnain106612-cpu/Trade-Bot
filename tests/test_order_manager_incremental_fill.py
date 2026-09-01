"""Coverage for OrderManager._record_incremental_fill in src/execution/order_manager.py.

ccxt reports `filled`/`average` cumulatively while the FSM takes
increments, so this method backs the newest piece's price out of the two
cumulative VWAPs. Every rejection path here is deliberately silent -- the
method runs alongside a live order's confirmation poll and must never be
the reason that loop dies -- which is exactly why the branches need
explicit tests rather than being inferred from the happy path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.execution.order_fsm import OrderFSM, OrderFSMError, OrderFSMState, OrderStatus
from src.execution.order_manager import OrderManager


def _fsm(
    status: OrderStatus = OrderStatus.FILLING,
    filled_qty: float = 0.0,
    average_fill_price: float | None = None,
    quantity: float = 10.0,
) -> OrderFSM:
    # filled_at_prices must agree with filled_qty/average_fill_price: the FSM
    # recomputes its VWAP from that list, so a state with a filled quantity but
    # no corresponding entries is not a state the FSM can actually be in, and
    # asserting against it would test the fixture rather than the code.
    filled_at_prices = []
    if filled_qty > 0.0:
        filled_at_prices = [(average_fill_price or 0.0, filled_qty)]
    state = OrderFSMState(
        order_id="o1",
        symbol="BTC/USDT",
        side="buy",
        quantity=quantity,
        status=status,
        filled_qty=filled_qty,
        filled_at_prices=filled_at_prices,
        average_fill_price=average_fill_price,
    )
    return OrderFSM(state)


def _manager() -> OrderManager:
    return OrderManager()


def test_ignores_order_not_in_filling_state():
    fsm = _fsm(status=OrderStatus.PENDING)
    _manager()._record_incremental_fill(fsm, {"filled": 5.0, "average": 100.0})
    assert fsm.state.filled_qty == 0.0


def test_ignores_missing_filled_field():
    fsm = _fsm()
    _manager()._record_incremental_fill(fsm, {"average": 100.0})
    assert fsm.state.filled_qty == 0.0


def test_ignores_missing_average_field():
    fsm = _fsm()
    _manager()._record_incremental_fill(fsm, {"filled": 5.0})
    assert fsm.state.filled_qty == 0.0


def test_ignores_non_numeric_fields():
    fsm = _fsm()
    _manager()._record_incremental_fill(fsm, {"filled": "not-a-number", "average": 100.0})
    assert fsm.state.filled_qty == 0.0


def test_ignores_no_forward_progress():
    fsm = _fsm(filled_qty=5.0, average_fill_price=100.0)
    # Same cumulative quantity as the FSM already holds -> delta of 0.
    _manager()._record_incremental_fill(fsm, {"filled": 5.0, "average": 100.0})
    assert fsm.state.filled_qty == 5.0


def test_ignores_backwards_progress():
    fsm = _fsm(filled_qty=5.0, average_fill_price=100.0)
    _manager()._record_incremental_fill(fsm, {"filled": 3.0, "average": 100.0})
    assert fsm.state.filled_qty == 5.0


def test_ignores_non_positive_cumulative_average():
    fsm = _fsm()
    _manager()._record_incremental_fill(fsm, {"filled": 5.0, "average": 0.0})
    assert fsm.state.filled_qty == 0.0


def test_records_first_fill():
    fsm = _fsm()
    _manager()._record_incremental_fill(fsm, {"filled": 4.0, "average": 100.0})
    assert fsm.state.filled_qty == 4.0
    assert fsm.state.average_fill_price == pytest.approx(100.0)


def test_derives_increment_price_from_cumulative_vwaps():
    # Already 2 @ 100 (value 200). Exchange now reports 4 @ 150 cumulative
    # (value 600), so the new 2 units must have gone off at 200, not at the
    # running average of 150.
    fsm = _fsm(filled_qty=2.0, average_fill_price=100.0)
    _manager()._record_incremental_fill(fsm, {"filled": 4.0, "average": 150.0})
    assert fsm.state.filled_qty == 4.0
    assert fsm.state.average_fill_price == pytest.approx(150.0)
    last_price, last_qty = fsm.state.filled_at_prices[-1]
    assert last_price == pytest.approx(200.0)
    assert last_qty == pytest.approx(2.0)


def test_ignores_non_positive_derived_price():
    # Cumulative value goes *down* while quantity goes up -> implied price of
    # the new piece is negative, which is unusable rather than merely odd.
    fsm = _fsm(filled_qty=2.0, average_fill_price=100.0)
    _manager()._record_incremental_fill(fsm, {"filled": 4.0, "average": 40.0})
    assert fsm.state.filled_qty == 2.0


def test_fsm_rejection_is_logged_not_raised():
    fsm = _fsm(filled_qty=2.0, average_fill_price=100.0, quantity=10.0)
    fsm.add_partial_fill = MagicMock(side_effect=OrderFSMError("overfill"))
    # Must not propagate -- this runs inside a live order's poll loop.
    _manager()._record_incremental_fill(fsm, {"filled": 4.0, "average": 150.0})
    fsm.add_partial_fill.assert_called_once()
