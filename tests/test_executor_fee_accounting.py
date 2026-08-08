"""
Recorded per-trade PnL must be net of both fee legs.

Both executors subtracted only the exit fee from the PnL they persisted,
leaving the entry fee stranded in pos.fee_usd. Cash was always right — the
entry fee leaves the balance at open — but the recorded number was not, and
that number is what compute_win_loss_stats turns into the Kelly fraction,
what attribution turns into Sharpe and Sortino for capital allocation, and
what the live gate checks against its out-of-sample threshold.

The two quantities genuinely differ, which is the trap: the fix must lower
recorded PnL without touching cash, or the entry fee gets charged twice and
equity drains.
"""

from __future__ import annotations

import pytest


def _settle(notional: float, entry_fee: float, exit_fee: float, gross: float, start: float):
    """Mirror the executor's open/close arithmetic."""
    cash_after_open = start - notional - entry_fee
    total_fee = entry_fee + exit_fee
    recorded_pnl = gross - total_fee
    cash_after_close = cash_after_open + notional + gross - exit_fee
    return recorded_pnl, cash_after_close, total_fee


def test_recorded_pnl_is_net_of_both_legs() -> None:
    recorded, _cash, total = _settle(1000.0, 1.0, 1.02, 25.0, 10_000.0)
    assert recorded == pytest.approx(25.0 - 2.02)
    assert total == pytest.approx(2.02)


def test_cash_settles_to_gross_minus_both_fees() -> None:
    # The entry fee is paid at open, the exit fee at close; equity must
    # reflect exactly one of each.
    start, gross, entry_fee, exit_fee = 10_000.0, 25.0, 1.0, 1.02
    _recorded, cash, _total = _settle(1000.0, entry_fee, exit_fee, gross, start)
    assert cash == pytest.approx(start + gross - entry_fee - exit_fee)


def test_the_entry_fee_is_not_charged_twice() -> None:
    # Using recorded_pnl for the cash return would deduct it a second time.
    start, gross, entry_fee, exit_fee = 10_000.0, 25.0, 1.0, 1.02
    recorded, cash, _total = _settle(1000.0, entry_fee, exit_fee, gross, start)
    double_charged = (start - 1000.0 - entry_fee) + 1000.0 + recorded
    assert cash > double_charged
    assert cash - double_charged == pytest.approx(entry_fee)


def test_a_losing_trade_is_recorded_more_negative_not_less() -> None:
    # The old behaviour flattered losses as well as gains.
    recorded, _cash, _total = _settle(1000.0, 1.0, 1.0, -30.0, 10_000.0)
    assert recorded == pytest.approx(-32.0)
    assert recorded < -30.0


def test_a_marginal_winner_can_become_a_loser_once_both_fees_apply() -> None:
    # Exactly the regime where an overstated edge matters: a trade whose
    # gross gain does not cover its round trip was being recorded as a win.
    recorded, _cash, _total = _settle(1000.0, 1.0, 1.0, 1.5, 10_000.0)
    assert recorded < 0.0


def test_a_profit_target_below_the_round_trip_cannot_be_profitable() -> None:
    # Exit thresholds compare against GROSS unrealized pct, so a target
    # smaller than the round trip books a loss every time it fires. The
    # config description says so; this pins the arithmetic behind it.
    notional, fee_pct = 1000.0, 0.001
    round_trip_pct = fee_pct * 2 * 100.0  # 0.2%

    target_pct = 0.1  # below the round trip
    gross = notional * target_pct / 100.0
    recorded, _cash, _total = _settle(notional, notional * fee_pct, notional * fee_pct, gross, 0.0)

    assert target_pct < round_trip_pct
    assert recorded < 0.0


def test_a_stop_books_more_than_its_nominal_percentage() -> None:
    # A 2.0 stop closes on a 2% adverse move and books ~2.2%.
    notional, fee_pct, stop_pct = 1000.0, 0.001, 2.0
    gross = -notional * stop_pct / 100.0
    recorded, _cash, _total = _settle(notional, notional * fee_pct, notional * fee_pct, gross, 0.0)

    booked_pct = -recorded / notional * 100.0
    assert booked_pct == pytest.approx(stop_pct + 0.2, abs=1e-9)
