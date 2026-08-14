"""
Tests for the GAP-003 performance-drift gate.

check_performance_drift() produced HALT_DRIFT, was tested, and was absent
from evaluate_all_gates() -- so drift was measured and reported and the
system kept trading on the drifted model. Detection was wired; the gate was
not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.config import TradingMode
from src.risk.gates import (
    GateStatus,
    RiskGateContext,
    check_performance_drift,
    evaluate_all_gates,
)
from src.risk.performance_drift import DriftDetected


def _detector(drifted: bool):
    detector = MagicMock()
    detector.check_drift.return_value = DriftDetected(
        drifted=drifted,
        reason="rolling sharpe 0.2 vs baseline 1.5",
        metric="sharpe",
        live_value=0.2,
        baseline_value=1.5,
        drift_pp=1.3,
    )
    detector.get_live_metrics.return_value = {"rolling_sharpe": 0.2}
    return detector


def _ctx(**overrides) -> RiskGateContext:
    """A context that passes every other gate, so only drift can fire."""
    base = {
        "capital_preservation_halted": False,
        "daily_pnl_usd": 0.0,
        "starting_equity_usd": 10_000.0,
        "consecutive_loss_count": 0,
        "regime_state": 1,
        "notional_usd": 100.0,
        "capital_usd": 10_000.0,
        "trading_mode": TradingMode.LIVE,
        "direction_gate_pass": True,
        "meta_gate_pass": True,
        "paper_trading_days": 999,
    }
    base.update(overrides)
    return RiskGateContext(**base)


class TestGateIsInTheLiveStack:
    def test_a_drifted_model_now_halts(self) -> None:
        """The gap: drift was detected, reported, and traded through."""
        result = evaluate_all_gates(_ctx(drift_detector=_detector(True)))
        assert result.passed is False
        assert result.status is GateStatus.HALT_DRIFT

    def test_a_healthy_model_passes(self) -> None:
        result = evaluate_all_gates(_ctx(drift_detector=_detector(False)))
        assert result.passed is True

    def test_no_detector_fails_open(self) -> None:
        """Absent a detector the gate is a no-op, not a halt."""
        assert evaluate_all_gates(_ctx(drift_detector=None)).passed is True

    def test_the_default_context_carries_no_detector(self) -> None:
        """Existing callers that never heard of this field are unaffected."""
        assert _ctx().drift_detector is None
        assert evaluate_all_gates(_ctx()).passed is True

    def test_the_halt_reason_names_the_drifted_metric(self) -> None:
        result = evaluate_all_gates(_ctx(drift_detector=_detector(True)))
        assert result.details["metric"] == "sharpe"
        assert "sharpe" in result.reason


class TestPaperIsExempt:
    def test_paper_trading_does_not_halt_on_drift(self) -> None:
        """
        Halting the paper track on drift would stop the run that is meant to
        be gathering evidence about whether the drift persists.
        """
        ctx = _ctx(trading_mode=TradingMode.PAPER, drift_detector=_detector(True))
        assert evaluate_all_gates(ctx).passed is True

    def test_live_trading_does_halt_on_the_same_detector(self) -> None:
        ctx = _ctx(trading_mode=TradingMode.LIVE, drift_detector=_detector(True))
        assert evaluate_all_gates(ctx).status is GateStatus.HALT_DRIFT


class TestGateOrdering:
    def test_a_harder_halt_still_wins(self) -> None:
        """
        Gates short-circuit on the first failure. The capital-preservation
        floor is the outermost control and must report before drift.
        """
        ctx = _ctx(capital_preservation_halted=True, drift_detector=_detector(True))
        assert evaluate_all_gates(ctx).status is GateStatus.HALT_CAPITAL_PRESERVATION

    def test_drift_reports_before_the_intelligence_gates(self) -> None:
        """
        Drift is a model-quality verdict from our own realized results; the
        intelligence gates are third-party signals that fail open. The
        stronger evidence should name the halt.
        """
        ctx = _ctx(drift_detector=_detector(True), exchange_stress_score=1.0)
        assert evaluate_all_gates(ctx).status is GateStatus.HALT_DRIFT


class TestDetectorContract:
    def test_the_gate_only_needs_check_drift_and_get_live_metrics(self) -> None:
        """
        RiskGateContext types this Any to avoid a gates -> performance_drift
        import edge, so the contract is structural and worth pinning.
        """
        detector = _detector(False)
        check_performance_drift(detector)
        detector.check_drift.assert_called_once()

    def test_a_detector_that_raises_halts_rather_than_passing(self) -> None:
        """
        Fails CLOSED, unlike the intelligence gates. A third-party feed being
        down says nothing about our model; a drift check that cannot run means
        the model's state is unknown, and check_drift() already returns
        drifted=False when it merely lacks data -- so reaching here is a real
        fault, not a cold start.
        """
        detector = MagicMock()
        detector.check_drift.side_effect = RuntimeError("baseline missing")
        result = check_performance_drift(detector)
        assert result.passed is False
        assert result.status is GateStatus.HALT_DRIFT
        assert "baseline missing" in result.reason

    def test_a_detector_fault_does_not_crash_the_whole_gate_stack(self) -> None:
        """Every other gate's verdict must still be computable."""
        detector = MagicMock()
        detector.check_drift.side_effect = RuntimeError("boom")
        result = evaluate_all_gates(_ctx(drift_detector=detector))
        assert result.status is GateStatus.HALT_DRIFT


def test_the_gate_is_actually_registered_in_the_stack() -> None:
    """A gate function nobody calls is the defect being fixed."""
    import inspect

    source = inspect.getsource(evaluate_all_gates)
    assert "check_performance_drift(ctx.drift_detector)" in source
