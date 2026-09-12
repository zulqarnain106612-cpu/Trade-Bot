"""
MODL-004 — model drift beyond threshold demotes the model.

A model keeps producing confident predictions in a regime it was never trained
on. Nothing about the output says so: the probabilities look the same, and the
only signal is that the realised performance has diverged from the baseline the
model was validated against.

So the demotion has to come from a comparison against that baseline, and the
comparison has to be **statistically guarded** — a run of five losses is not
drift, and a detector that halts on one is a detector the operator turns off.

The behaviours pinned here:

1. A healthy model is not demoted.
2. A genuinely degraded model is.
3. Too little evidence is not drift — the detector says "no drift", not
   "drift", when it cannot tell.
4. Drift reaches the gate stack as a halt, live only, and a detector that
   cannot run fails **closed** (also `INV-006`).
"""

from __future__ import annotations

import pytest

from src.config import TradingMode
from src.risk.drift_integration import DriftIntegrationAdapter
from src.risk.gates import GateStatus, check_performance_drift, evaluate_all_gates
from src.risk.performance_drift import (
    DriftDetected,
    PerformanceBaseline,
    PerformanceDriftDetector,
)


def baseline(**overrides) -> PerformanceBaseline:
    values = {
        "train_sharpe": 2.0,
        "oos_sharpe": 1.8,
        # FRACTIONS, not percentages -- see PerformanceBaseline's units note.
        "train_accuracy": 0.62,
        "oos_accuracy": 0.58,
        "train_win_rate": 0.55,
        "max_drawdown_pct": 0.08,
        "trades_in_backtest": 800,
    }
    values.update(overrides)
    return PerformanceBaseline(**values)


def feed(
    detector: PerformanceDriftDetector,
    n: int,
    *,
    pnl: float,
    correct: bool,
    equity: float = 100_000.0,
) -> None:
    """Record `n` live trades with a fixed outcome shape."""
    for _ in range(n):
        detector.record_trade_outcome(
            pnl_usd=pnl,
            predicted_prob=0.8,
            actual_direction=1 if correct else -1,
            current_equity=equity,
            starting_equity=100_000.0,
        )


def healthy_baseline() -> PerformanceBaseline:
    """
    A baseline whose Sharpe is a *per-trade* figure.

    `PerformanceDriftDetector` computes live Sharpe over a rolling window of
    50 trades, so the baseline it is compared against has to be on the same
    scale. A backtest's annualised 1.8 is not, and using it here would report
    drift on every healthy model -- which is the trap, not the test.
    """
    return baseline(train_sharpe=0.5, oos_sharpe=0.4)


def feed_healthy(detector: PerformanceDriftDetector, n: int = 120) -> None:
    """
    A model performing at its baseline: ~60% correct, positive expectancy.

    Interleaved rather than fed in blocks. The rolling windows hold the last
    50 trades, so 60 wins followed by 40 losses leaves a window that is 80%
    losses -- a "healthy" fixture that actually describes a collapsing model.
    """
    for i in range(n):
        win = i % 5 < 3
        detector.record_trade_outcome(
            pnl_usd=150.0 if win else -50.0,
            predicted_prob=0.8,
            actual_direction=1 if win else -1,
            current_equity=100_000.0 + i * 20.0,
            starting_equity=100_000.0,
        )


class TestAHealthyModelIsNotDemoted:
    def test_a_fresh_detector_reports_no_drift(self):
        assert not PerformanceDriftDetector(baseline()).check_drift().drifted

    def test_a_model_performing_at_baseline_is_not_demoted(self):
        detector = PerformanceDriftDetector(healthy_baseline())
        feed_healthy(detector)
        assert not detector.check_drift().drifted

    def test_the_gate_passes_a_healthy_detector(self):
        detector = PerformanceDriftDetector(healthy_baseline())
        feed_healthy(detector)
        assert check_performance_drift(detector).passed


class TestTooLittleEvidenceIsNotDrift:
    @pytest.mark.parametrize("n", [1, 5, 15])
    def test_a_short_losing_run_is_not_drift(self, n):
        # A detector that halts on five losses is a detector the operator
        # turns off, and then it protects nothing at all.
        detector = PerformanceDriftDetector(baseline())
        feed(detector, n, pnl=-500.0, correct=False)
        assert not detector.check_drift().drifted

    def test_the_trade_counter_is_monotonic(self):
        # Lets a caller distinguish "new evidence arrived" from "the same
        # rolling window was polled again".
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 10, pnl=100.0, correct=True)
        assert detector.total_live_trades == 10
        feed(detector, 5, pnl=100.0, correct=True)
        assert detector.total_live_trades == 15


class TestADegradedModelIsDemoted:
    def test_sustained_losses_and_wrong_calls_trigger_drift(self):
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 120, pnl=-400.0, correct=False, equity=55_000.0)
        result = detector.check_drift()
        assert result.drifted
        assert result.metric
        assert result.reason

    def test_the_drift_report_names_the_metric_and_the_gap(self):
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 120, pnl=-400.0, correct=False, equity=55_000.0)
        result = detector.check_drift()
        assert result.baseline_value != result.live_value
        assert isinstance(result.drift_pp, float)

    def test_drift_halts_the_live_stack(self, passing_gate_ctx, risk_cfg):
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 120, pnl=-400.0, correct=False, equity=55_000.0)
        ctx = passing_gate_ctx(trading_mode=TradingMode.LIVE, drift_detector=detector)
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_DRIFT

    def test_drift_does_not_halt_the_paper_track(self, passing_gate_ctx, risk_cfg):
        # Halting paper on drift would stop the run that is gathering the
        # evidence about whether the drift persists.
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 120, pnl=-400.0, correct=False, equity=55_000.0)
        ctx = passing_gate_ctx(trading_mode=TradingMode.PAPER, drift_detector=detector)
        assert evaluate_all_gates(ctx, risk_cfg).passed


class TestTheDetectorFailsClosed:
    def test_a_detector_that_raises_halts(self):
        class _Broken:
            def check_drift(self):
                raise RuntimeError("metrics store unavailable")

        result = check_performance_drift(_Broken())
        assert not result.passed
        assert result.status is GateStatus.HALT_DRIFT

    def test_an_absent_detector_passes_and_says_why(self):
        result = check_performance_drift(None)
        assert result.passed
        assert result.details["reason"] == "drift_detector_not_enabled"


class TestTheIntegrationAdapter:
    def test_it_reports_no_drift_without_a_detector(self):
        report = DriftIntegrationAdapter(None).check_drift()
        assert report["drifted"] is False

    def test_it_surfaces_a_detected_drift(self):
        detector = PerformanceDriftDetector(baseline())
        feed(detector, 120, pnl=-400.0, correct=False, equity=55_000.0)
        report = DriftIntegrationAdapter(detector).check_drift()
        assert report["drifted"] is True

    def test_it_surfaces_a_healthy_model(self):
        detector = PerformanceDriftDetector(healthy_baseline())
        feed_healthy(detector)
        assert DriftIntegrationAdapter(detector).check_drift()["drifted"] is False


class TestTheBaselineIsAnArtifact:
    def test_it_serialises_every_field_a_rollback_needs(self):
        as_dict = baseline().to_dict()
        for key in (
            "train_sharpe",
            "oos_sharpe",
            "train_accuracy",
            "oos_accuracy",
            "train_win_rate",
            "max_drawdown_pct",
            "trades_in_backtest",
            "set_at_ms",
        ):
            assert key in as_dict

    def test_the_detector_exposes_its_baseline(self):
        original = baseline()
        assert PerformanceDriftDetector(original).baseline is original

    @pytest.mark.parametrize(
        "field",
        ["train_accuracy", "oos_accuracy", "train_win_rate", "max_drawdown_pct"],
    )
    def test_a_percentage_shaped_value_is_refused_at_construction(self, field):
        # Found by writing this suite. The docstring said these fields were
        # percentages; every threshold, every live value and every reason
        # string in the module treats them as fractions. A caller who
        # believed the docstring produced a pooled proportion above 1 inside
        # the two-proportion z-test, math.sqrt of a negative, and a
        # ValueError reading "expected a nonnegative input" from deep inside
        # a risk control -- which the gate then turned into a halt with a
        # reason nobody could act on.
        with pytest.raises(ValueError, match="FRACTION, not a percentage"):
            baseline(**{field: 58.0})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_a_non_finite_rate_is_refused(self, bad):
        with pytest.raises(ValueError, match="must be finite"):
            baseline(train_accuracy=bad)

    def test_a_drift_result_defaults_to_no_drift(self):
        # The default matters: a partially-constructed result must not read
        # as a detection and halt trading on nothing.
        assert not DriftDetected(drifted=False).drifted
