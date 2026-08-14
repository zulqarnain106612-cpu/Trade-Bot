"""
Live max drawdown must be peak-relative, matching the baseline it is compared to.

record_trade_outcome divided (peak - current) by starting_equity. The result
is compared in _check_drawdown_drift against the backtest's
max_drawdown_pct, which is peak-relative by construction -- so the two sides
of the comparison used different denominators and diverged by exactly the
account's growth factor. On an account that had doubled, a true 30% drawdown
from peak reported as 60% and tripped the >10pp expansion halt. The halt
therefore fired hardest on the accounts that had performed best.

Every other drawdown in the project (gates.DrawdownTracker,
tuning.stress_simulator, risk.capital_preservation_floor) already divides by
peak; this module was the outlier.
"""

from __future__ import annotations

import pytest

from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector


def _detector() -> PerformanceDriftDetector:
    return PerformanceDriftDetector(
        PerformanceBaseline(
            train_sharpe=1.2,
            oos_sharpe=1.0,
            train_accuracy=0.58,
            oos_accuracy=0.55,
            train_win_rate=0.55,
            max_drawdown_pct=0.20,
            trades_in_backtest=500,
        )
    )


def _feed(det, equity: float, start: float = 10_000.0) -> None:
    det.record_trade_outcome(
        pnl_usd=1.0,
        predicted_prob=0.6,
        actual_direction=1,
        current_equity=equity,
        starting_equity=start,
    )


def test_drawdown_on_a_grown_account_is_measured_from_the_peak() -> None:
    det = _detector()
    _feed(det, 20_000.0)  # peak
    _feed(det, 14_000.0)  # 30% off peak

    assert det.get_live_metrics()["max_live_drawdown_pct"] == pytest.approx(0.30)


def test_the_old_basis_would_have_doubled_that_figure() -> None:
    # Pins the magnitude of the bug: start-relative gives 0.60 for the same
    # move, which alone exceeds the 20% baseline by 40pp.
    det = _detector()
    _feed(det, 20_000.0)
    _feed(det, 14_000.0)

    start_relative = (20_000.0 - 14_000.0) / 10_000.0
    assert start_relative == pytest.approx(0.60)
    assert det.get_live_metrics()["max_live_drawdown_pct"] < start_relative


def test_a_flat_account_is_unaffected_by_the_change() -> None:
    # When the peak never rises above the starting equity the two bases
    # coincide, which is why the bug survived: the common case is right.
    det = _detector()
    _feed(det, 10_000.0)
    _feed(det, 8_000.0)

    assert det.get_live_metrics()["max_live_drawdown_pct"] == pytest.approx(0.20)


def test_max_drawdown_is_a_high_water_mark_not_the_latest_value() -> None:
    det = _detector()
    _feed(det, 10_000.0)
    _feed(det, 7_000.0)  # 30%
    _feed(det, 9_500.0)  # recovered

    assert det.get_live_metrics()["max_live_drawdown_pct"] == pytest.approx(0.30)


def test_a_new_peak_resets_the_denominator() -> None:
    det = _detector()
    _feed(det, 10_000.0)
    _feed(det, 9_000.0)  # 10% off 10k
    _feed(det, 40_000.0)  # new peak
    _feed(det, 36_000.0)  # 10% off 40k

    assert det.get_live_metrics()["max_live_drawdown_pct"] == pytest.approx(0.10)


def test_an_invalid_starting_equity_still_discards_the_sample() -> None:
    det = _detector()
    _feed(det, 10_000.0)
    _feed(det, 5_000.0, start=0.0)

    assert det.get_live_metrics()["max_live_drawdown_pct"] == pytest.approx(0.0)
