"""
A terminal order reporting zero filled quantity is not a completed fill.

order_manager resolved the filled size as
`float(confirmed.get("filled") or confirmed.get("amount", 0))`. The comment
directly beneath that line (UI-009) explains exactly why `or` is wrong for
the fill *price* -- an explicit 0.0 is falsy and silently becomes the next
field or a default -- but the quantity above it still used it. So an
exchange returning status "closed" with filled=0.0 fell through to the
requested `amount` and the order was recorded as completely filled.

A real fill is never 0, so missing and explicitly-zero are now rejected the
same way a missing price already was: critical log, FSM to FAILED, raise.
"""

from __future__ import annotations

import pytest


def _resolve(confirmed: dict) -> tuple[float | None, float | None]:
    """Mirror the price/quantity resolution in place_order_with_fsm."""
    filled_field = confirmed.get("filled")
    raw_filled = filled_field if filled_field is not None else confirmed.get("amount")

    average_field = confirmed.get("average")
    price_field = confirmed.get("price")
    raw_avg = average_field if average_field is not None else price_field
    return raw_avg, raw_filled


def _rejected(confirmed: dict) -> tuple[bool, bool]:
    raw_avg, raw_filled = _resolve(confirmed)
    bad_price = raw_avg is None or float(raw_avg) <= 0.0
    bad_qty = raw_filled is None or float(raw_filled) <= 0.0
    return bad_price, bad_qty


def test_a_zero_filled_quantity_is_rejected() -> None:
    bad_price, bad_qty = _rejected({"filled": 0.0, "average": 100.0, "amount": 2.0})

    assert bad_qty is True
    assert bad_price is False


def test_the_old_expression_would_have_accepted_it_as_a_full_fill() -> None:
    confirmed = {"filled": 0.0, "average": 100.0, "amount": 2.0}
    old = float(confirmed.get("filled") or confirmed.get("amount", 0))

    assert old == 2.0  # the requested size, not what filled
    assert _resolve(confirmed)[1] == 0.0


def test_a_missing_filled_field_still_falls_back_to_amount() -> None:
    _bad_price, bad_qty = _rejected({"average": 100.0, "amount": 2.0})

    assert bad_qty is False
    assert _resolve({"average": 100.0, "amount": 2.0})[1] == 2.0


def test_a_partial_quantity_is_accepted_at_face_value() -> None:
    _bad_price, bad_qty = _rejected({"filled": 0.4, "average": 100.0, "amount": 2.0})

    assert bad_qty is False
    assert _resolve({"filled": 0.4, "average": 100.0, "amount": 2.0})[1] == 0.4


def test_a_zero_price_is_still_rejected() -> None:
    # The original UI-009 behaviour must be unchanged.
    bad_price, _bad_qty = _rejected({"filled": 2.0, "average": 0.0, "price": 100.0})

    assert bad_price is True


def test_price_falls_back_from_average_to_price() -> None:
    raw_avg, _ = _resolve({"filled": 2.0, "price": 100.0})
    assert raw_avg == 100.0


def test_both_fields_can_be_bad_at_once() -> None:
    bad_price, bad_qty = _rejected({"filled": 0.0, "average": 0.0})

    assert (bad_price, bad_qty) == (True, True)


def test_a_well_formed_fill_is_accepted() -> None:
    bad_price, bad_qty = _rejected({"filled": 2.0, "average": 100.0, "amount": 2.0})

    assert (bad_price, bad_qty) == (False, False)
    raw_avg, raw_filled = _resolve({"filled": 2.0, "average": 100.0, "amount": 2.0})
    assert float(raw_avg) * float(raw_filled) == pytest.approx(200.0)


def test_a_missing_amount_and_missing_filled_is_rejected_not_defaulted() -> None:
    # The old expression defaulted amount to 0 and then recorded that 0 as
    # the fill; now the absence is caught.
    _bad_price, bad_qty = _rejected({"average": 100.0})

    assert bad_qty is True
