"""
EXEC-003, EXEC-004, INV-003 — the exchange-response contract.

An exchange is a boundary this project does not own. The venue can add a
status, rename a field, or return a terminal status with no fill price, and it
will do so without warning. So the parse is checked for **totality** — every
input produces an answer — and for **refusal** — an input the contract cannot
vouch for produces one that says so rather than a plausible default.

The invariant underneath all of it:

> Unknown exchange order state cannot become FILLED without reconciliation.

That is expressed structurally rather than by a comment: there is no entry
mapping `UNKNOWN` to an FSM state, so `fsm_status` returns None however the
caller is written.
"""

from __future__ import annotations

import math

import pytest

from src.execution.exchange_contract import (
    FSM_STATUS,
    STATUS_ALIASES,
    ExchangeOrderStatus,
    OrderUpdate,
    normalise_status,
    parse_order,
)
from src.execution.order_fsm import OrderStatus


def response(**overrides) -> dict:
    base = {
        "id": "1234",
        "symbol": "BTC/USDT",
        "status": "closed",
        "filled": 0.5,
        "amount": 0.5,
        "average": 50_000.0,
        "price": 50_000.0,
        "remaining": 0.0,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _ABSENT}


_ABSENT = object()


class TestStatusNormalisation:
    @pytest.mark.parametrize(("raw", "expected"), sorted(STATUS_ALIASES.items()))
    def test_every_declared_alias_maps(self, raw, expected):
        assert normalise_status(raw)[0] is expected

    @pytest.mark.parametrize("raw", ["CLOSED", " closed ", "Filled", "CANCELED"])
    def test_case_and_whitespace_do_not_matter(self, raw):
        assert normalise_status(raw)[0] is not ExchangeOrderStatus.UNKNOWN

    @pytest.mark.parametrize(
        "raw", ["partially_cancelled", "cancel_pending", "settled", "", "closed_out"]
    )
    def test_an_unrecognised_status_is_unknown_not_guessed(self, raw):
        # "cancel_pending" is not "cancelled" and "closed_out" is not
        # "closed". A prefix rule would say they were, which is why the
        # alias table is exact.
        assert normalise_status(raw)[0] is ExchangeOrderStatus.UNKNOWN

    @pytest.mark.parametrize("raw", [None, 5, {"status": "closed"}, ["closed"]])
    def test_a_non_string_status_is_unknown(self, raw):
        assert normalise_status(raw)[0] is ExchangeOrderStatus.UNKNOWN

    def test_the_original_string_is_preserved_for_the_log(self):
        assert normalise_status("  WeIrD  ")[1] == "  WeIrD  "


class TestAWellFormedResponse:
    def test_a_completed_fill_parses(self):
        update = parse_order(response(), expected_symbol="BTC/USDT")
        assert update.status is ExchangeOrderStatus.FILLED
        assert update.filled_qty == 0.5
        assert update.average_price == 50_000.0
        assert update.is_usable
        assert not update.needs_reconciliation
        assert update.reason == ""

    def test_an_open_order_parses(self):
        update = parse_order(response(status="open", filled=0.1, remaining=0.4))
        assert update.status is ExchangeOrderStatus.OPEN
        assert update.fsm_status is OrderStatus.FILLING

    def test_a_cancellation_parses_without_needing_a_fill(self):
        update = parse_order(response(status="canceled", filled=_ABSENT, average=_ABSENT))
        assert update.status is ExchangeOrderStatus.CANCELLED
        assert update.is_usable
        assert update.fsm_status is OrderStatus.CANCELLED

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("open", OrderStatus.FILLING),
            ("closed", OrderStatus.FILLED),
            ("canceled", OrderStatus.CANCELLED),
            ("rejected", OrderStatus.FAILED),
            ("expired", OrderStatus.FAILED),
        ],
    )
    def test_each_recognised_status_maps_to_one_fsm_state(self, status, expected):
        assert parse_order(response(status=status)).fsm_status is expected


class TestUnknownCannotBecomeFilled:
    def test_an_unknown_status_needs_reconciliation(self):
        update = parse_order(response(status="settled"))
        assert update.status is ExchangeOrderStatus.UNKNOWN
        assert update.needs_reconciliation
        assert "unrecognised status" in update.reason

    def test_an_unknown_status_has_no_fsm_state_at_all(self):
        # The invariant, structurally: there is no mapping to fall through to.
        assert ExchangeOrderStatus.UNKNOWN not in FSM_STATUS
        assert parse_order(response(status="settled")).fsm_status is None

    def test_an_unknown_status_carrying_a_perfect_fill_still_reconciles(self):
        # The dangerous shape: everything looks right except the one field
        # that says what happened.
        update = parse_order(response(status="totally_done", filled=1.0, average=50_000.0))
        assert update.fsm_status is None
        assert update.needs_reconciliation


class TestATerminalFillMustCarryAUsableFill:
    @pytest.mark.parametrize("filled", [_ABSENT, 0.0, -1.0, math.nan, math.inf, "n/a"])
    def test_an_unusable_filled_quantity_is_refused(self, filled):
        # `order.get("filled") or order.get("amount")` treats an explicit 0.0
        # exactly like a missing field, because 0.0 is falsy. A real fill is
        # never zero, so both are refused here rather than one being quietly
        # substituted for the other.
        update = parse_order(response(filled=filled))
        assert update.needs_reconciliation
        assert "filled quantity" in update.reason

    @pytest.mark.parametrize("price", [_ABSENT, 0.0, -1.0, math.nan, "free"])
    def test_an_unusable_fill_price_is_refused(self, price):
        update = parse_order(response(average=price, price=price))
        assert update.needs_reconciliation
        assert "fill price" in update.reason

    def test_a_zero_average_does_not_fall_through_to_price(self):
        # An explicit 0.0 average is a broken response, not an absent field,
        # so it must not be replaced by `price`.
        update = parse_order(response(average=0.0, price=50_000.0))
        assert update.needs_reconciliation

    def test_a_missing_average_does_fall_through_to_price(self):
        update = parse_order(response(average=_ABSENT, price=50_000.0))
        assert update.is_usable
        assert update.average_price == 50_000.0

    def test_a_non_terminal_status_is_not_required_to_carry_a_fill(self):
        assert parse_order(response(status="open", filled=_ABSENT, average=_ABSENT)).is_usable


class TestIdentity:
    def test_a_missing_id_is_not_a_problem(self):
        # The caller fetched this order by id and already knows which one it
        # asked about, so an absent echo is a thin response, not a wrong one.
        update = parse_order(response(id=_ABSENT), expected_order_id="1234")
        assert update.is_usable
        assert update.order_id == "1234"

    def test_a_mismatched_id_is_refused(self):
        # A response about a different order. Acting on it books the wrong
        # fill against this one.
        update = parse_order(response(id="9999"), expected_order_id="1234")
        assert update.needs_reconciliation
        assert "order id mismatch" in update.reason

    def test_no_expected_id_means_no_id_check(self):
        assert parse_order(response(id="9999")).is_usable

    def test_a_response_for_another_symbol_is_refused(self):
        # Not a malformed field -- the wrong order. Booking its fill against
        # this one would be a position in the wrong asset.
        update = parse_order(response(symbol="ETH/USDT"), expected_symbol="BTC/USDT")
        assert update.needs_reconciliation
        assert "symbol mismatch" in update.reason

    def test_no_expected_symbol_means_no_symbol_check(self):
        assert parse_order(response(symbol="ETH/USDT")).is_usable

    def test_a_missing_symbol_falls_back_to_the_expected_one(self):
        update = parse_order(response(symbol=_ABSENT), expected_symbol="BTC/USDT")
        assert update.symbol == "BTC/USDT"
        assert update.is_usable


class TestTotality:
    @pytest.mark.parametrize("garbage", [None, [], "closed", 42, {"unexpected": "shape"}, {}])
    def test_every_input_produces_an_answer(self, garbage):
        # A parse that raises on some inputs pushes the decision into the
        # caller's except clause, where "keep waiting" and "stop and
        # reconcile" collapse into one.
        update = parse_order(garbage)
        assert isinstance(update, OrderUpdate)
        assert update.needs_reconciliation

    def test_a_non_mapping_says_what_it_got(self):
        assert "got list" in parse_order([]).reason

    def test_every_problem_is_reported_not_just_the_first(self):
        update = parse_order(
            response(id="9999", status="mystery", filled=0.0), expected_order_id="1234"
        )
        assert len(update.problems) >= 2

    def test_the_raw_response_is_preserved(self):
        raw = response()
        assert parse_order(raw).raw is raw

    def test_the_update_is_frozen(self):
        update = parse_order(response())
        with pytest.raises(AttributeError):
            update.status = ExchangeOrderStatus.CANCELLED


class TestRemaining:
    def test_zero_remaining_is_a_legitimate_value(self):
        # Unlike a fill quantity, zero remaining is exactly what a completed
        # order reports, so it must not be coerced to None.
        assert parse_order(response(remaining=0.0)).remaining_qty == 0.0

    @pytest.mark.parametrize("remaining", [-1.0, math.nan, "some", _ABSENT])
    def test_an_unusable_remaining_is_none_rather_than_a_guess(self, remaining):
        assert parse_order(response(remaining=remaining)).remaining_qty is None

    def test_a_bad_remaining_does_not_by_itself_force_reconciliation(self):
        # It is informational: the fill quantity is what books the position.
        assert parse_order(response(remaining=-1.0)).is_usable
