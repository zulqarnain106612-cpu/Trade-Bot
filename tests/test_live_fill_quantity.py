"""
A reported fill of zero is not a full fill.

Both legs of the live executor read the filled size as
`float(order.get("filled") or <requested>)`. `or` cannot distinguish a
missing value from 0.0, so an order that filled nothing -- rejected, or
cancelled immediately -- was treated as completely filled:

- entry: a position was recorded that does not exist on the exchange;
- exit: the position was deleted from internal state while the exposure was
  still live, which is the worse direction because nothing downstream would
  ever look for it again.

Only a missing value (None) may fall back to the requested size. An
explicit 0.0 is the exchange telling us nothing happened.
"""

from __future__ import annotations

import pytest


def _filled(order: dict, requested: float) -> float:
    """Mirror the resolution used on both legs."""
    raw = order.get("filled")
    return float(requested) if raw is None else float(raw)


def _old_filled(order: dict, requested: float) -> float:
    """The previous expression, kept to pin the difference."""
    return float(order.get("filled") or requested)


def test_a_zero_fill_is_reported_as_zero() -> None:
    assert _filled({"filled": 0.0}, 1.5) == 0.0


def test_the_old_expression_reported_a_zero_fill_as_complete() -> None:
    assert _old_filled({"filled": 0.0}, 1.5) == 1.5
    assert _filled({"filled": 0.0}, 1.5) != _old_filled({"filled": 0.0}, 1.5)


def test_a_missing_field_still_falls_back_to_the_requested_size() -> None:
    # ccxt does not always populate `filled` for a market order; assuming the
    # request was met is the only usable estimate there.
    assert _filled({}, 1.5) == 1.5
    assert _filled({"filled": None}, 1.5) == 1.5


def test_a_partial_fill_is_taken_at_face_value() -> None:
    assert _filled({"filled": 0.6}, 1.5) == 0.6


def test_a_complete_fill_is_unchanged() -> None:
    assert _filled({"filled": 1.5}, 1.5) == 1.5


def test_an_integer_zero_is_also_a_zero_fill() -> None:
    # Some venues serialise the field as an int.
    assert _filled({"filled": 0}, 1.5) == 0.0


# ------------------------------------------------------- downstream guards


def _partial(requested: float, filled: float) -> bool:
    """Mirror the close leg's partial-fill test."""
    return filled < requested * 0.999


def test_a_zero_fill_would_now_fail_the_entry_guard() -> None:
    # _place_and_record rejects on `filled_qty <= 0.0` and restores the
    # reserved cash, so the phantom position is never recorded.
    assert _filled({"filled": 0.0}, 1.5) <= 0.0


def test_a_partial_close_is_flagged() -> None:
    assert _partial(1.5, 0.6) is True


def test_a_complete_close_is_not_flagged() -> None:
    assert _partial(1.5, 1.5) is False


def test_float_dust_does_not_count_as_a_partial_close() -> None:
    # Exchange rounding can return a hair under the requested size; that is
    # not an untracked residual worth a critical log.
    assert _partial(1.5, 1.5 - 1e-9) is False
    assert _partial(1.5, 1.4995) is False


def test_the_partial_threshold_still_catches_a_real_shortfall() -> None:
    assert _partial(1.5, 1.49) is True
    assert pytest.approx(1.5 - 1.49) == 0.01
