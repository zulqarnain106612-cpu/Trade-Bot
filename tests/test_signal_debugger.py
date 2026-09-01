"""
Pure-logic tests for src/diagnostics/signal_debugger.py.

Covers FeatureDriftMonitor, ModelDegradationTracker, and LabelShiftDetector
without any I/O or exchange connectivity.
"""

from __future__ import annotations

import math

import pytest

from src.diagnostics.signal_debugger import (
    ACCURACY_DROP_THRESHOLD,
    KS_DRIFT_THRESHOLD,
    LABEL_SHIFT_MIN_TRADES,
    LABEL_SHIFT_THRESHOLD,
    ROLLING_ACCURACY_THRESHOLD,
    ROLLING_SHARPE_THRESHOLD,
    FeatureDriftMonitor,
    FeatureDriftRecord,
    LabelShiftDetector,
    LabelShiftRecord,
    ModelDegradationTracker,
    get_label_shift_detector,
)

# ---------------------------------------------------------------------------
# FeatureDriftMonitor
# ---------------------------------------------------------------------------


class TestFeatureDriftMonitorBaseline:
    def test_set_baseline_stores_mean(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("rsi", [10.0, 20.0, 30.0])
        # After setting baseline, pushing 50+ values and checking gives stats
        # Indirectly verify via check_all not raising
        for v in [20.0] * 60:
            m.push("rsi", v)
        records = m.check_all()
        assert len(records) == 1
        assert records[0].feature == "rsi"

    def test_set_baseline_empty_noop(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("rsi", [])
        for v in [20.0] * 60:
            m.push("rsi", v)
        # No baseline stored → check_all returns nothing
        assert m.check_all() == []

    def test_push_ignores_inf(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("vol", [1.0] * 10)
        for _ in range(60):
            m.push("vol", 1.0)
        m.push("vol", math.inf)
        # Should not raise; inf is silently dropped
        records = m.check_all()
        assert len(records) == 1

    def test_push_ignores_nan(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("vol", [1.0] * 10)
        for _ in range(60):
            m.push("vol", 1.0)
        m.push("vol", float("nan"))
        records = m.check_all()
        assert len(records) == 1

    def test_check_all_requires_50_samples(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("feat", [0.0] * 10)
        for _ in range(49):
            m.push("feat", 0.0)
        # Only 49 live samples — below threshold
        assert m.check_all() == []

    def test_no_drift_when_live_matches_baseline(self) -> None:
        baseline = [float(i) for i in range(100)]
        m = FeatureDriftMonitor()
        m.set_baseline("feat", baseline)
        mean_val = sum(baseline) / len(baseline)
        for _ in range(60):
            m.push("feat", mean_val)
        records = m.check_all()
        assert len(records) == 1
        assert records[0].drifted is False
        assert records[0].ks_statistic < KS_DRIFT_THRESHOLD

    def test_drift_detected_on_large_shift(self) -> None:
        baseline = [0.0] * 100
        m = FeatureDriftMonitor()
        m.set_baseline("feat", baseline)
        # Live mean is far from baseline (std=0), denom clamped to 1e-9
        for _ in range(60):
            m.push("feat", 10.0)
        records = m.check_all()
        assert len(records) == 1
        assert records[0].drifted is True

    def test_drift_record_has_correct_fields(self) -> None:
        m = FeatureDriftMonitor()
        m.set_baseline("x", [5.0] * 100)
        for _ in range(60):
            m.push("x", 5.0)
        rec = m.check_all()[0]
        assert isinstance(rec, FeatureDriftRecord)
        assert rec.feature == "x"
        assert isinstance(rec.ks_statistic, float)
        assert isinstance(rec.train_mean, float)
        assert isinstance(rec.live_mean, float)


# ---------------------------------------------------------------------------
# ModelDegradationTracker
# ---------------------------------------------------------------------------


def _make_tracker_with_resolved(
    n: int, correct_fraction: float, train_accuracy: float = 0.70
) -> ModelDegradationTracker:
    tracker = ModelDegradationTracker(window=300)
    tracker.set_training_metrics(train_accuracy, 0.65)
    for i in range(n):
        tracker.record_prediction(0.8, 0.7)
        direction = 1 if i < int(n * correct_fraction) else 0
        tracker.resolve_last(direction)
    return tracker


class TestModelDegradationTracker:
    def test_set_training_metrics_stored(self) -> None:
        t = ModelDegradationTracker()
        t.set_training_metrics(0.72, 0.68)
        report = t.check_degradation()
        assert report["train_accuracy"] == 0.72

    def test_no_degradation_when_accuracy_high(self) -> None:
        # 90% correct vs 70% training → no degradation
        t = _make_tracker_with_resolved(50, correct_fraction=0.9)
        report = t.check_degradation()
        assert report["degraded"] is False

    def test_degradation_flagged_on_accuracy_drop(self) -> None:
        # 40% correct vs 70% training → 30-point drop > ACCURACY_DROP_THRESHOLD
        t = _make_tracker_with_resolved(50, correct_fraction=0.4)
        report = t.check_degradation()
        assert report["degraded"] is True
        assert report["retrain_recommended"] is True

    def test_degradation_flagged_below_accuracy_floor(self) -> None:
        # 50% correct vs 55% training → drop is small but below ROLLING_ACCURACY_THRESHOLD
        t = _make_tracker_with_resolved(50, correct_fraction=0.50, train_accuracy=0.55)
        report = t.check_degradation()
        # 50% live < ROLLING_ACCURACY_THRESHOLD (0.52)
        assert report["degraded"] is True

    def test_live_accuracy_none_below_20_resolved(self) -> None:
        t = ModelDegradationTracker()
        t.set_training_metrics(0.7, 0.65)
        for _ in range(15):
            t.record_prediction(0.8, 0.7)
            t.resolve_last(1)
        assert t.live_accuracy() is None

    def test_prediction_stats_returns_correct_keys(self) -> None:
        t = ModelDegradationTracker()
        stats = t.prediction_stats()
        assert "total_predictions" in stats
        assert "accuracy" in stats
        assert "predictions_per_sec" in stats

    def test_accuracy_none_when_no_resolved(self) -> None:
        t = ModelDegradationTracker()
        stats = t.prediction_stats()
        assert stats["accuracy"] is None

    def test_rolling_sharpe_none_below_20_trades(self) -> None:
        t = ModelDegradationTracker()
        for v in [100.0] * 19:
            t.record_trade_result(v)
        assert t.rolling_sharpe() is None

    def test_rolling_sharpe_computes_for_20_trades(self) -> None:
        t = ModelDegradationTracker()
        for v in [1.0, -1.0] * 10:
            t.record_trade_result(v)
        sharpe = t.rolling_sharpe()
        assert sharpe is not None
        assert isinstance(sharpe, float)

    def test_rolling_sharpe_zero_for_all_zero_pnl(self) -> None:
        t = ModelDegradationTracker()
        for _ in range(25):
            t.record_trade_result(0.0)
        assert t.rolling_sharpe() == 0.0

    def test_check_degradation_no_train_metrics(self) -> None:
        t = ModelDegradationTracker()
        for _ in range(30):
            t.record_prediction(0.8, 0.7)
            t.resolve_last(1)
        report = t.check_degradation()
        assert report["train_accuracy"] is None
        assert report["degraded"] is False

    def test_sharpe_degradation_triggers_flag(self) -> None:
        t = ModelDegradationTracker()
        t.set_training_metrics(0.65, 0.60)
        # High live accuracy but very negative Sharpe
        for _ in range(30):
            t.record_prediction(0.8, 0.7)
            t.resolve_last(1)  # all correct
        # negative PnL stream → sharpe < ROLLING_SHARPE_THRESHOLD
        for _ in range(25):
            t.record_trade_result(-5.0)
        report = t.check_degradation()
        assert report["degraded"] is True

    def test_resolve_last_correct_prediction(self) -> None:
        t = ModelDegradationTracker()
        t.record_prediction(0.8, 0.7)  # p_long > 0.5 → predicts direction=1
        t.resolve_last(1)  # correct
        stats = t.prediction_stats()
        assert stats["correct_predictions"] == 1

    def test_resolve_last_incorrect_prediction(self) -> None:
        t = ModelDegradationTracker()
        t.record_prediction(0.8, 0.7)
        t.resolve_last(0)  # wrong
        stats = t.prediction_stats()
        assert stats["correct_predictions"] == 0


# ---------------------------------------------------------------------------
# LabelShiftDetector
# ---------------------------------------------------------------------------


class TestLabelShiftDetector:
    def test_no_baseline_returns_none(self) -> None:
        d = LabelShiftDetector()
        for _ in range(50):
            d.record_trade(100.0)
        assert d.check() is None

    def test_baseline_invalid_raises(self) -> None:
        d = LabelShiftDetector()
        with pytest.raises(ValueError):
            d.set_baseline(1.5)
        with pytest.raises(ValueError):
            d.set_baseline(-0.1)

    def test_below_min_trades_returns_none(self) -> None:
        d = LabelShiftDetector()
        d.set_baseline(0.6)
        for _ in range(LABEL_SHIFT_MIN_TRADES - 1):
            d.record_trade(100.0)
        assert d.check() is None

    def test_no_drift_when_win_rate_matches_baseline(self) -> None:
        d = LabelShiftDetector()
        d.set_baseline(0.6)
        # 60% wins → no drop
        for _ in range(50):
            d.record_trade(100.0)
        for _ in range(33):
            d.record_trade(-100.0)
        rec = d.check()
        assert rec is not None
        assert rec.drifted is False

    def test_drift_detected_when_win_rate_drops(self) -> None:
        d = LabelShiftDetector()
        d.set_baseline(0.7)
        # 40% wins → drop = 0.30 > LABEL_SHIFT_THRESHOLD (0.15)
        for _ in range(20):
            d.record_trade(100.0)
        for _ in range(30):
            d.record_trade(-100.0)
        rec = d.check()
        assert rec is not None
        assert rec.drifted is True

    def test_label_shift_record_fields(self) -> None:
        d = LabelShiftDetector()
        d.set_baseline(0.6)
        for _ in range(30):
            d.record_trade(100.0)
        for _ in range(20):
            d.record_trade(-100.0)
        rec = d.check()
        assert isinstance(rec, LabelShiftRecord)
        assert 0.0 <= rec.live_win_rate <= 1.0
        assert rec.n_trades == 50
        assert isinstance(rec.win_rate_drop, float)

    def test_singleton_get_label_shift_detector(self) -> None:
        d1 = get_label_shift_detector()
        d2 = get_label_shift_detector()
        assert d1 is d2

    def test_just_below_threshold_not_drifted(self) -> None:
        d = LabelShiftDetector()
        # baseline 0.60, live 0.50 → drop = 0.10 < LABEL_SHIFT_THRESHOLD (0.15) → not drifted
        d.set_baseline(0.60)
        for _ in range(50):
            d.record_trade(100.0)  # 50% wins (0.50 live_win_rate)
        for _ in range(50):
            d.record_trade(-100.0)
        rec = d.check()
        assert rec is not None
        assert rec.drifted is False  # drop = 0.10 < 0.15 threshold

    def test_record_trade_loss_counts_as_zero(self) -> None:
        d = LabelShiftDetector()
        d.set_baseline(0.5)
        for _ in range(50):
            d.record_trade(-1.0)  # all losses
        rec = d.check()
        assert rec is not None
        assert rec.live_win_rate == 0.0


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_ks_drift_threshold_reasonable(self) -> None:
        assert 0.0 < KS_DRIFT_THRESHOLD < 1.0

    def test_accuracy_drop_threshold_reasonable(self) -> None:
        assert 0.0 < ACCURACY_DROP_THRESHOLD < 1.0

    def test_rolling_sharpe_threshold_positive(self) -> None:
        assert ROLLING_SHARPE_THRESHOLD > 0.0

    def test_rolling_accuracy_floor_above_chance(self) -> None:
        assert ROLLING_ACCURACY_THRESHOLD > 0.50

    def test_label_shift_min_trades_positive(self) -> None:
        assert LABEL_SHIFT_MIN_TRADES > 0

    def test_label_shift_threshold_positive(self) -> None:
        assert LABEL_SHIFT_THRESHOLD > 0.0
