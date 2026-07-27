"""Tests for the v10 CUSUM strategy decay detector."""

from __future__ import annotations

from src.risk.strategy_decay import CusumDecayDetector


def test_stable_performance_stays_below_threshold() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.2, decision_threshold=5.0)
    for _ in range(50):
        detector.update(1.0)
    assert not detector.is_decayed
    assert detector.cusum_statistic == 0.0


def test_persistent_underperformance_triggers_decay() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.1, decision_threshold=3.0)
    for _ in range(20):
        detector.update(0.0)
    assert detector.is_decayed


def test_transient_dip_does_not_trigger_decay() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.5, decision_threshold=10.0)
    detector.update(1.0)
    detector.update(0.0)  # one dip
    detector.update(1.2)  # recovers
    assert not detector.is_decayed


def test_observation_count_increments() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0)
    detector.update(1.0)
    detector.update(1.0)
    assert detector.observation_count == 2


def test_reset_clears_state() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.1, decision_threshold=1.0)
    for _ in range(10):
        detector.update(0.0)
    assert detector.is_decayed
    detector.reset()
    assert not detector.is_decayed
    assert detector.cusum_statistic == 0.0
    assert detector.observation_count == 0


def test_cusum_never_goes_negative() -> None:
    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.1)
    detector.update(10.0)  # way above baseline
    assert detector.cusum_statistic == 0.0
