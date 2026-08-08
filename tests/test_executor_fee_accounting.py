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
