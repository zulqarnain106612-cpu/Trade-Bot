"""Edge-case coverage for src/risk/performance_drift.py.

Targets the guard branches the full-suite run listed as missing: the
zero-peak drawdown guard, the too-few-samples returns in the rolling
Sortino/Sharpe accessors, the zero-downside-deviation return, and the
Sortino/accuracy drift early exits.
"""

from __future__ import annotations

from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector


def _baseline(**overrides) -> PerformanceBaseline:
    kwargs = dict(
        train_sharpe=1.5,
        oos_sharpe=1.2,
        train_accuracy=0.65,
        oos_accuracy=0.60,
        train_win_rate=0.55,
        max_drawdown_pct=10.0,
        trades_in_backtest=500,
        train_sortino=2.0,
        oos_sortino=1.8,
    )
    kwargs.update(overrides)
    return PerformanceBaseline(**kwargs)


def _detector(**overrides) -> PerformanceDriftDetector:
    return PerformanceDriftDetector(_baseline(**overrides))


def _fill_window(detector: PerformanceDriftDetector, pnls: list[float]) -> None:
    for pnl in pnls:
        detector.record_trade_outcome(
            pnl_usd=pnl,
            predicted_prob=0.8 if pnl > 0 else 0.2,
            actual_direction=1 if pnl > 0 else -1,
            current_equity=10_000.0 + pnl,
            starting_equity=10_000.0,
        )


def test_drawdown_tracking_skipped_on_invalid_starting_equity():
    detector = _detector()
    detector.record_trade_outcome(
        pnl_usd=-1.0,
        predicted_prob=0.4,
        actual_direction=1,
        current_equity=100.0,
        starting_equity=0.0,
    )
    assert detector._max_live_drawdown_pct == 0.0


def test_drawdown_tracking_skipped_when_peak_never_set():
    detector = _detector()
    # starting_equity is valid (so the first guard passes), but equity never
    # rises above 0, so the peak stays 0 and the peak-relative division is
    # skipped rather than dividing by zero.
    detector.record_trade_outcome(
        pnl_usd=-1.0,
        predicted_prob=0.4,
        actual_direction=1,
        current_equity=0.0,
        starting_equity=10_000.0,
    )
    assert detector._live_equity_peak == 0.0
    assert detector._max_live_drawdown_pct == 0.0


def test_total_live_trades_counts_monotonically():
    detector = _detector()
    assert detector.total_live_trades == 0
    _fill_window(detector, [1.0, -1.0, 1.0])
    assert detector.total_live_trades == 3


def test_rolling_sortino_returns_none_below_minimum_window():
    detector = _detector()
    _fill_window(detector, [1.0] * 5)
    assert detector.current_rolling_sortino() is None


def test_rolling_sortino_returns_none_when_no_downside():
    detector = _detector()
    # All-positive P&L -> no losses -> downside deviation of 0 -> None,
    # rather than dividing by zero.
    _fill_window(detector, [10.0] * 25)
    assert detector.current_rolling_sortino() is None


def test_rolling_sortino_computes_with_mixed_pnl():
    detector = _detector()
    _fill_window(detector, [10.0, -5.0] * 15)
    result = detector.current_rolling_sortino()
    assert result is not None
    assert isinstance(result, float)


def test_rolling_sharpe_returns_none_below_minimum_window():
    detector = _detector()
    _fill_window(detector, [1.0] * 5)
    assert detector.current_rolling_sharpe() is None


def test_sortino_drift_no_baseline_returns_not_drifted():
    detector = _detector(oos_sortino=0.0)
    _fill_window(detector, [10.0, -5.0] * 15)
    assert detector._check_sortino_drift().drifted is False


def test_sortino_drift_returns_not_drifted_when_live_sortino_unavailable():
    detector = _detector()
    _fill_window(detector, [1.0] * 5)  # below the 20-sample window
    assert detector._check_sortino_drift().drifted is False


def test_sortino_drift_flags_a_real_drop():
    detector = _detector(oos_sortino=50.0)  # unreachably high baseline
    _fill_window(detector, [1.0, -5.0] * 15)
    result = detector._check_sortino_drift()
    assert result.drifted is True
    assert "Sortino drifted" in result.reason


def test_sortino_drift_not_flagged_when_live_meets_baseline():
    # Baseline low enough that live Sortino clears it -> falls through to the
    # final not-drifted return rather than the drift branch.
    detector = _detector(oos_sortino=0.01)
    _fill_window(detector, [10.0, -1.0] * 15)
    assert detector._check_sortino_drift().drifted is False


def test_sharpe_drift_returns_not_drifted_below_minimum_window():
    detector = _detector()
    _fill_window(detector, [1.0] * 5)
    assert detector._check_sharpe_drift().drifted is False


def test_accuracy_drift_returns_not_drifted_below_minimum_window():
    detector = _detector()
    _fill_window(detector, [1.0] * 5)
    assert detector._check_accuracy_drift().drifted is False


def test_accuracy_drift_computes_over_a_full_window():
    detector = _detector(oos_accuracy=0.95)
    # Every prediction wrong: prob > 0.5 predicts long, actual is short.
    for _ in range(25):
        detector.record_trade_outcome(
            pnl_usd=-1.0,
            predicted_prob=0.9,
            actual_direction=-1,
            current_equity=10_000.0,
            starting_equity=10_000.0,
        )
    assert detector._check_accuracy_drift().drifted is True
