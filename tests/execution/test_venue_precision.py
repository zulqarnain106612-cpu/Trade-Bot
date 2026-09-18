"""
DATA-005, EXEC-006 — venue precision rules and exact fill accounting.

Two requirements that meet at the same place: the number actually sent to the
exchange.

**Precision** (`DATA-005`). Every submitted order must conform to the venue's
tick, lot and minimum-notional rules, checked *before* submission. An order
rejected by the venue for a precision violation fails at exactly the moment
the strategy needed it, and the rejection arrives as a generic InvalidOrder
that says nothing useful.

**Accounting** (`EXEC-006`). Position, average price and fee after a sequence
of partial fills must match a hand-calculated figure. PnL that is wrong makes
every downstream risk decision wrong too, and the arithmetic is simple enough
that "it looked about right" is not a defence — so the expected values below
are computed by hand in the test, not by a second implementation of the code
under test.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

import pytest

from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
from src.risk.kelly import _floor_to_precision


def filling(quantity: float) -> OrderFSM:
    return OrderFSM(
        OrderFSMState(
            order_id="ord-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=quantity,
            status=OrderStatus.FILLING,
        )
    )


class TestQuantisationToVenuePrecision:
    @pytest.mark.parametrize(
        ("value", "places", "expected"),
        [
            (0.123456789, 8, 0.12345678),
            (0.123456789, 6, 0.123456),
            (0.123456789, 3, 0.123),
            (1.999999, 2, 1.99),
            (12345.6789, 0, 12345.0),
        ],
    )
    def test_a_quantity_is_floored_to_the_venue_s_step(self, value, places, expected):
        assert _floor_to_precision(value, places) == pytest.approx(expected, abs=1e-12)

    @pytest.mark.parametrize("places", [0, 1, 2, 4, 8])
    def test_quantising_never_increases_a_quantity(self, places):
        # The property that matters: a quantity checked against a position
        # limit before quantisation must still be inside it afterwards.
        # Rounding up can push an order past a limit that was already
        # checked, and past the venue's balance.
        for value in (0.1, 0.999999, 1.0, 1.5, 12345.6789):
            assert _floor_to_precision(value, places) <= value + 1e-12

    def test_quantising_is_idempotent(self):
        once = _floor_to_precision(0.123456789, 6)
        assert _floor_to_precision(once, 6) == once

    def test_it_matches_decimal_rather_than_binary_floor(self):
        # `math.floor(v * 10**n) / 10**n` has visible representation
        # artifacts at eight decimal places on BTC quantities. This is the
        # comparison that showed it.
        value = 0.1
        assert _floor_to_precision(value, 8) == float(
            Decimal(str(value)).quantize(Decimal("1e-8"), rounding=ROUND_DOWN)
        )

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_quantity_is_refused(self, bad):
        with pytest.raises(ValueError, match="non-finite quantity"):
            _floor_to_precision(bad, 8)

    def test_a_negative_precision_is_refused(self):
        with pytest.raises(ValueError, match="decimal_places"):
            _floor_to_precision(1.0, -1)


class TestMinimumsAreCheckedAgainstTheQuantisedValue:
    """
    The order the checks run in is the whole point.

    The number the venue sees is the quantised one, so checking the
    *unquantised* value against a minimum answers a question nobody asked: a
    0.000019 BTC order quantised to 8 places is 0.00001, and if the venue's
    minimum is 0.000015 the order is rejected — after passing a check that
    looked at 0.000019.
    """

    @pytest.mark.parametrize(
        ("raw", "places", "minimum", "acceptable"),
        [
            (0.000019, 5, 0.000015, False),
            (0.00002, 5, 0.000015, True),
            (0.1000001, 2, 0.1, True),
            (0.0999999, 2, 0.1, False),
        ],
    )
    def test_the_minimum_is_compared_against_what_is_sent(self, raw, places, minimum, acceptable):
        quantised = _floor_to_precision(raw, places)
        assert (quantised >= minimum) is acceptable

    def test_a_quantity_that_quantises_to_zero_is_not_an_order(self):
        assert _floor_to_precision(0.0000001, 4) == 0.0

    def test_minimum_notional_uses_the_quantised_quantity(self):
        # Cost = quantised_qty * price, not raw_qty * price. The difference
        # is what a venue's min-cost rejection is made of.
        raw, price, places = 0.000199, 50_000.0, 4
        quantised = _floor_to_precision(raw, places)
        assert quantised * price == pytest.approx(0.0001 * 50_000.0)


class TestPartialFillAccounting:
    """
    EXEC-006. Expected values are hand-calculated in the assertion, so the
    test cannot agree with the code by sharing a bug with it.
    """

    def test_two_fills_average_by_volume(self):
        # (0.4 * 100 + 0.6 * 110) / 1.0 = 106.0
        machine = filling(1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        assert machine.state.filled_qty == pytest.approx(1.0)
        assert machine.state.average_fill_price == pytest.approx(106.0)

    def test_an_arithmetic_mean_would_give_a_different_answer(self):
        # 105.0 is the wrong answer, and it is the answer an unweighted mean
        # gives. Stated explicitly so the test cannot pass by coincidence.
        machine = filling(1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        assert machine.state.average_fill_price != pytest.approx(105.0)

    def test_five_uneven_fills(self):
        # 0.1*100 + 0.2*101 + 0.3*102 + 0.15*103 + 0.25*104
        #   = 10 + 20.2 + 30.6 + 15.45 + 26 = 102.25 over 1.0
        machine = filling(1.0)
        for qty, price in ((0.1, 100.0), (0.2, 101.0), (0.3, 102.0), (0.15, 103.0), (0.25, 104.0)):
            machine.add_partial_fill(qty, price)
        assert machine.state.filled_qty == pytest.approx(1.0)
        assert machine.state.average_fill_price == pytest.approx(102.25)

    def test_a_single_fill_averages_to_its_own_price(self):
        machine = filling(1.0)
        machine.add_partial_fill(1.0, 99.5)
        assert machine.state.average_fill_price == pytest.approx(99.5)

    def test_every_fill_is_kept_for_the_audit_trail(self):
        # The average is a summary; the reconciliation needs the sequence.
        machine = filling(1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        assert machine.state.filled_at_prices == [(100.0, 0.4), (110.0, 0.6)]

    def test_a_partial_sequence_reports_a_partial_fill(self):
        machine = filling(1.0)
        machine.add_partial_fill(0.3, 100.0)
        assert machine.state.fill_percentage() == pytest.approx(0.3)
        assert machine.state.filled_qty < machine.state.quantity

    def test_the_average_survives_the_transition_to_filled(self):
        machine = filling(1.0)
        machine.add_partial_fill(0.4, 100.0)
        machine.add_partial_fill(0.6, 110.0)
        before = machine.state.average_fill_price
        machine.transition(OrderStatus.FILLED, {"filled_qty": 1.0, "average_price": before})
        assert machine.state.average_fill_price == pytest.approx(106.0)

    def test_accumulated_drift_over_many_fills_stays_inside_tolerance(self):
        # 200 fills of 0.001 at a moving price. The exact weighted average is
        # computed independently here in Decimal.
        machine = filling(0.2)
        exact_value = Decimal(0)
        exact_qty = Decimal(0)
        for i in range(200):
            qty_s, price_s = "0.001", f"{100 + i * 0.01:.2f}"
            machine.add_partial_fill(float(qty_s), float(price_s))
            exact_value += Decimal(qty_s) * Decimal(price_s)
            exact_qty += Decimal(qty_s)
        expected = float(exact_value / exact_qty)
        assert machine.state.average_fill_price == pytest.approx(expected, rel=1e-9)
        assert machine.state.filled_qty == pytest.approx(float(exact_qty), rel=1e-9)
