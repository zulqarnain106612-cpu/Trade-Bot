"""Tests for intelligence gates in src/risk/gates.py (GAP-015 coverage)."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.risk.gates import (
    GateStatus,
    check_exchange_stress,
    check_performance_drift,
    check_whale_activity,
)


# ---------------------------------------------------------------------------
# check_exchange_stress
# ---------------------------------------------------------------------------


class TestCheckExchangeStress:
    def test_none_score_passes(self) -> None:
        r = check_exchange_stress(None)
        assert r.passed
        assert r.details["exchange_stress_gate"] == "skipped_no_data"

    def test_low_score_passes_with_no_action(self) -> None:
        r = check_exchange_stress(0.30)
        assert r.passed
        assert r.details["stress_action"] == "none"

    def test_score_above_reduce_threshold_passes_with_reduce_suggested(self) -> None:
        r = check_exchange_stress(0.60, stress_halt_threshold=0.75, stress_reduce_threshold=0.50)
        assert r.passed
        assert r.details["stress_action"] == "reduce_suggested"

    def test_score_above_halt_threshold_halts(self) -> None:
        r = check_exchange_stress(0.80, stress_halt_threshold=0.75)
        assert not r.passed
        assert r.status == GateStatus.HALT_EXCHANGE_STRESS
        assert r.details["stress_action"] == "halt"

    def test_score_exactly_at_halt_threshold_does_not_halt(self) -> None:
        # Uses strict > comparison
        r = check_exchange_stress(0.75, stress_halt_threshold=0.75)
        assert r.passed

    def test_custom_thresholds_respected(self) -> None:
        r = check_exchange_stress(0.95, stress_halt_threshold=0.90, stress_reduce_threshold=0.70)
        assert not r.passed
        assert r.status == GateStatus.HALT_EXCHANGE_STRESS

    def test_score_stored_in_details(self) -> None:
        r = check_exchange_stress(0.40)
        assert r.details["exchange_stress_score"] == 0.4


# ---------------------------------------------------------------------------
# check_whale_activity
# ---------------------------------------------------------------------------


class TestCheckWhaleActivity:
    def test_none_ratio_passes(self) -> None:
        r = check_whale_activity(None)
        assert r.passed
        assert r.details["whale_gate"] == "skipped_no_data"

    def test_above_threshold_passes(self) -> None:
        r = check_whale_activity(1.2, sell_threshold=0.85)
        assert r.passed
        assert r.details["whale_action"] == "none"

    def test_at_threshold_passes(self) -> None:
        # Uses strict < comparison
        r = check_whale_activity(0.85, sell_threshold=0.85)
        assert r.passed

    def test_below_threshold_triggers(self) -> None:
        # Blocks by default; RISK_WHALE_GATE_ADVISORY opts into halving
        # instead. See _whale_outcome -- flipping a live risk control from
        # veto to size-reduction is an operator decision, so the default
        # stayed "block". tests/test_whale_gate_advisory.py covers both
        # postures; this asserts the default one.
        r = check_whale_activity(0.70, sell_threshold=0.85)
        assert not r.passed
        assert r.status == GateStatus.REDUCE_WHALE_ACTIVITY
        assert r.details["whale_action"] == "block"

    def test_below_threshold_reduces_when_advisory(self) -> None:
        r = check_whale_activity(0.70, sell_threshold=0.85, advisory=True, advisory_scalar=0.5)
        assert r.status == GateStatus.REDUCE_WHALE_ACTIVITY
        assert r.details["whale_action"] == "reduce_to_50%"

    def test_ratio_stored_in_details(self) -> None:
        r = check_whale_activity(1.5)
        assert r.details["whale_buy_sell_ratio"] == 1.5

    def test_zero_ratio_reduces(self) -> None:
        r = check_whale_activity(0.0)
        assert not r.passed
        assert r.status == GateStatus.REDUCE_WHALE_ACTIVITY


# ---------------------------------------------------------------------------
# check_performance_drift
# ---------------------------------------------------------------------------


def _drift_detector(drifted: bool, reason: str = "", metric: str = "sharpe") -> MagicMock:
    d = MagicMock()
    drift_result = MagicMock()
    drift_result.drifted = drifted
    drift_result.reason = reason
    drift_result.metric = metric
    drift_result.live_value = 0.5
    drift_result.baseline_value = 1.5
    drift_result.drift_pp = -1.0
    d.check_drift.return_value = drift_result
    d.get_live_metrics.return_value = {
        "total_live_trades": 50,
        "rolling_sharpe": 0.8,
        "rolling_winrate": 0.52,
        "rolling_accuracy": 0.54,
        "max_live_drawdown_pct": 3.5,
    }
    return d


class TestCheckPerformanceDrift:
    def test_none_detector_passes(self) -> None:
        r = check_performance_drift(None)
        assert r.passed
        assert r.details["reason"] == "drift_detector_not_enabled"

    def test_no_drift_passes(self) -> None:
        r = check_performance_drift(_drift_detector(drifted=False))
        assert r.passed
        assert r.details["total_trades"] == 50

    def test_drift_detected_halts(self) -> None:
        r = check_performance_drift(_drift_detector(drifted=True, reason="sharpe dropped"))
        assert not r.passed
        assert r.status == GateStatus.HALT_DRIFT
        assert r.details["metric"] == "sharpe"

    def test_drift_details_populated(self) -> None:
        r = check_performance_drift(_drift_detector(drifted=True))
        assert "live_value" in r.details
        assert "baseline_value" in r.details
        assert "drift_pp" in r.details
