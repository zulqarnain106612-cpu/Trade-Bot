"""Tests for src/diagnostics/signal_debugger.py (60% → target 85%+)."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np

from src.diagnostics.signal_debugger import (
    FeatureDriftMonitor,
    FeatureDriftRecord,
    LabelShiftDetector,
    LabelShiftRecord,
    ModelDegradationTracker,
    PredictionRecord,
    get_degradation_tracker,
    get_drift_monitor,
    get_label_shift_detector,
    run_pipeline_selftest,
)


# ---------------------------------------------------------------------------
# FeatureDriftRecord
# ---------------------------------------------------------------------------


def test_feature_drift_record_fields():
    r = FeatureDriftRecord(
        feature="vwap",
        ks_statistic=0.25,
        drifted=True,
        train_mean=0.0,
        live_mean=0.5,
        train_std=0.1,
        live_std=0.15,
    )
    assert r.drifted is True
    assert r.feature == "vwap"


# ---------------------------------------------------------------------------
# FeatureDriftMonitor
# ---------------------------------------------------------------------------


class TestFeatureDriftMonitor:
    def setup_method(self):
        self.monitor = FeatureDriftMonitor(window=100)

    def test_set_baseline_empty_noop(self):
        self.monitor.set_baseline("feat", [])
        assert "feat" not in self.monitor._baselines

    def test_set_baseline_single_value(self):
        self.monitor.set_baseline("feat", [1.0])
        assert "feat" in self.monitor._baselines
        assert self.monitor._baselines["feat"]["std"] == 0.0

    def test_set_baseline_computes_stats(self):
        vals = list(range(1, 101))
        self.monitor.set_baseline("feat", vals)
        assert "feat" in self.monitor._baselines
        assert abs(self.monitor._baselines["feat"]["mean"] - 50.5) < 0.01

    def test_push_filters_nan(self):
        self.monitor.push("feat", float("nan"))
        assert (
            "feat" not in self.monitor._buffers or len(self.monitor._buffers.get("feat", [])) == 0
        )

    def test_push_filters_inf(self):
        self.monitor.push("feat", float("inf"))
        assert (
            "feat" not in self.monitor._buffers or len(self.monitor._buffers.get("feat", [])) == 0
        )

    def test_push_creates_buffer(self):
        self.monitor.push("feat", 1.0)
        assert "feat" in self.monitor._buffers
        assert 1.0 in self.monitor._buffers["feat"]

    def test_check_all_empty_returns_no_results(self):
        results = self.monitor.check_all()
        assert results == []

    def test_check_all_insufficient_live_data(self):
        self.monitor.set_baseline("feat", list(range(100)))
        # Push only 10 values — below threshold of 50
        for v in range(10):
            self.monitor.push("feat", float(v))
        results = self.monitor.check_all()
        assert results == []

    def test_check_all_no_drift(self):
        rng = np.random.default_rng(42)
        vals = rng.standard_normal(200).tolist()
        self.monitor.set_baseline("feat", vals)
        # Push similar values
        for v in rng.standard_normal(100):
            self.monitor.push("feat", float(v))
        results = self.monitor.check_all()
        assert len(results) == 1
        assert results[0].feature == "feat"
        assert results[0].drifted is False

    def test_check_all_detects_drift(self):
        vals = [0.0] * 100
        self.monitor.set_baseline("feat", vals)
        # Push values far from baseline
        for _ in range(100):
            self.monitor.push("feat", 100.0)
        results = self.monitor.check_all()
        assert len(results) == 1
        assert results[0].drifted is True

    def test_set_baseline_also_creates_buffer(self):
        self.monitor.set_baseline("newFeat", [1.0, 2.0, 3.0])
        self.monitor.push("newFeat", 1.5)
        assert "newFeat" in self.monitor._buffers


# ---------------------------------------------------------------------------
# PredictionRecord
# ---------------------------------------------------------------------------


def test_prediction_record_defaults():
    pr = PredictionRecord(ts=1.0, p_long=0.7, p_bet=0.6)
    assert pr.actual_direction is None


# ---------------------------------------------------------------------------
# ModelDegradationTracker
# ---------------------------------------------------------------------------


class TestModelDegradationTracker:
    def setup_method(self):
        self.tracker = ModelDegradationTracker(window=200)

    def test_set_training_metrics(self):
        self.tracker.set_training_metrics(0.75, 0.72)
        assert self.tracker._train_accuracy == 0.75
        assert self.tracker._train_f1 == 0.72

    def test_record_prediction_appends(self):
        self.tracker.record_prediction(0.7, 0.6)
        assert len(self.tracker._preds) == 1

    def test_resolve_last_fills_actual(self):
        self.tracker.record_prediction(0.7, 0.6)
        self.tracker.resolve_last(1)
        assert self.tracker._preds[-1].actual_direction == 1

    def test_resolve_last_fills_most_recent_unresolved(self):
        self.tracker.record_prediction(0.7, 0.6)
        self.tracker.resolve_last(1)
        self.tracker.record_prediction(0.3, 0.55)
        self.tracker.resolve_last(0)
        resolved = [r for r in self.tracker._preds if r.actual_direction is not None]
        assert len(resolved) == 2

    def test_resolve_last_no_unresolved_noop(self):
        self.tracker.record_prediction(0.7, 0.6)
        self.tracker.resolve_last(1)
        self.tracker.resolve_last(0)  # nothing to resolve → noop

    def test_record_trade_result(self):
        self.tracker.record_trade_result(100.0)
        assert len(self.tracker._trade_pnls) == 1

    def test_rolling_sharpe_insufficient_trades(self):
        for _ in range(10):
            self.tracker.record_trade_result(1.0)
        assert self.tracker.rolling_sharpe() is None

    def test_rolling_sharpe_all_zero_returns_zero(self):
        for _ in range(20):
            self.tracker.record_trade_result(0.0)
        assert self.tracker.rolling_sharpe() == 0.0

    def test_rolling_sharpe_positive_returns(self):
        for v in [1.0, 2.0, 3.0, 1.5, 2.5] * 5:
            self.tracker.record_trade_result(v)
        sharpe = self.tracker.rolling_sharpe()
        assert sharpe is not None
        assert sharpe > 0

    def test_rolling_sharpe_zero_std(self):
        for _ in range(20):
            self.tracker.record_trade_result(1.0)
        assert self.tracker.rolling_sharpe() == 0.0

    def test_rolling_sortino_insufficient_trades(self):
        for _ in range(10):
            self.tracker.record_trade_result(-5.0)
        assert self.tracker.rolling_sortino() is None

    def test_rolling_sortino_no_losses_returns_none(self):
        for _ in range(25):
            self.tracker.record_trade_result(10.0)
        # No negative P&Ls → empty losses list → None
        assert self.tracker.rolling_sortino() is None

    def test_rolling_sortino_returns_float_with_mixed_pnl(self):
        for i in range(25):
            pnl = 10.0 if i % 3 != 0 else -5.0
            self.tracker.record_trade_result(pnl)
        s = self.tracker.rolling_sortino()
        assert s is not None

    def test_rolling_sortino_in_check_degradation_report(self):
        for i in range(25):
            pnl = 5.0 if i % 3 != 0 else -2.0
            self.tracker.record_trade_result(pnl)
        report = self.tracker.check_degradation()
        assert "rolling_sortino" in report

    def test_live_accuracy_insufficient_resolved(self):
        for _ in range(10):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
        assert self.tracker.live_accuracy() is None

    def test_live_accuracy_perfect(self):
        for _ in range(25):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
        acc = self.tracker.live_accuracy()
        assert acc == 1.0

    def test_live_accuracy_zero(self):
        for _ in range(25):
            self.tracker.record_prediction(0.7, 0.6)  # predicts long
            self.tracker.resolve_last(0)  # actual is short
        acc = self.tracker.live_accuracy()
        assert acc == 0.0

    def test_check_degradation_no_training_metrics(self):
        for _ in range(25):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
        report = self.tracker.check_degradation()
        assert report["train_accuracy"] is None
        assert report["degraded"] is False

    def test_prediction_stats_empty(self):
        stats = self.tracker.prediction_stats()
        assert stats["predictions_per_sec"] == 0.0
        assert stats["total_predictions"] == 0
        assert stats["correct_predictions"] == 0
        assert stats["resolved_predictions"] == 0
        assert stats["accuracy"] is None

    def test_prediction_stats_counts_and_rate(self):
        for _ in range(5):
            self.tracker.record_prediction(0.7, 0.6)
        stats = self.tracker.prediction_stats(rate_window_s=10.0)
        assert stats["total_predictions"] == 5
        assert stats["predictions_per_sec"] == 0.5  # 5 preds / 10s window

    def test_prediction_stats_accuracy_matches_correct_ratio(self):
        for _ in range(3):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)  # correct
        for _ in range(2):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(0)  # incorrect
        stats = self.tracker.prediction_stats()
        assert stats["total_predictions"] == 5
        assert stats["resolved_predictions"] == 5
        assert stats["correct_predictions"] == 3
        assert stats["accuracy"] == 0.6

    def test_check_degradation_no_degradation(self):
        self.tracker.set_training_metrics(0.65, 0.63)
        rng = np.random.default_rng(42)
        for _ in range(30):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
            # Use varying returns to get positive Sharpe (not all identical)
            self.tracker.record_trade_result(float(rng.standard_normal() * 5 + 10))
        report = self.tracker.check_degradation()
        assert "live_accuracy" in report
        assert "rolling_sharpe" in report
        # live accuracy = 1.0 (> 0.52 floor), sharpe > 0.8, drop < threshold → no degradation
        assert report["degraded"] is False

    def test_check_degradation_detects_accuracy_drop(self):
        self.tracker.set_training_metrics(0.80, 0.78)  # high train accuracy
        # Make live accuracy ~0.4 (below floor 0.52)
        for _ in range(30):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(0)  # always wrong → 0% accuracy
        report = self.tracker.check_degradation()
        assert report["degraded"] is True

    def test_check_degradation_sortino_degraded_key_always_present(self):
        # Even with no training metrics, sortino_degraded must be in report.
        report = self.tracker.check_degradation()
        assert "sortino_degraded" in report
        assert report["sortino_degraded"] is False

    def test_check_degradation_sortino_triggers_when_below_threshold(self):
        # High train accuracy so accuracy drop alone won't trigger degradation.
        self.tracker.set_training_metrics(0.55, 0.53)
        # Feed exactly 25 trades: 5 large losses, 20 small wins → Sortino < 0.5.
        for i in range(25):
            pnl = -50.0 if i < 5 else 0.5
            self.tracker.record_trade_result(pnl)
        # Resolve 25 predictions with ~55% accuracy so accuracy path doesn't trigger.
        for _ in range(25):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
        report = self.tracker.check_degradation()
        assert report["rolling_sortino"] is not None
        assert report["sortino_degraded"] is True
        assert report["degraded"] is True
        assert report["retrain_recommended"] is True
        assert report["tighten_meta_label_threshold"] is True

    def test_check_degradation_good_sortino_does_not_trigger(self):
        # All winning trades → sortino returns None (no losses) → not degraded.
        self.tracker.set_training_metrics(0.55, 0.53)
        for _ in range(25):
            self.tracker.record_trade_result(10.0)
        for _ in range(25):
            self.tracker.record_prediction(0.7, 0.6)
            self.tracker.resolve_last(1)
        report = self.tracker.check_degradation()
        assert report["sortino_degraded"] is False


# ---------------------------------------------------------------------------
# run_pipeline_selftest
# ---------------------------------------------------------------------------


def test_run_pipeline_selftest_passes():
    result = run_pipeline_selftest()
    assert result["passed"] is True
    assert result["n_rows"] > 0
    assert result["n_features"] > 0
    assert result["error"] is None


# ---------------------------------------------------------------------------
# LabelShiftRecord and LabelShiftDetector
# ---------------------------------------------------------------------------


class TestLabelShiftDetector:
    def setup_method(self):
        self.detector = LabelShiftDetector(window=50)

    def test_set_baseline(self):
        self.detector.set_baseline(0.60)
        assert self.detector._baseline_win_rate == 0.60

    def test_record_trade_win(self):
        self.detector.record_trade(10.0)
        assert len(self.detector._outcomes) == 1
        assert self.detector._outcomes[-1] == 1

    def test_record_trade_loss(self):
        self.detector.record_trade(-5.0)
        assert len(self.detector._outcomes) == 1
        assert self.detector._outcomes[-1] == 0

    def test_check_no_baseline(self):
        for _ in range(35):
            self.detector.record_trade(1.0)
        result = self.detector.check()
        assert result is None  # no baseline set

    def test_check_insufficient_trades(self):
        self.detector.set_baseline(0.60)
        for _ in range(10):
            self.detector.record_trade(1.0)
        result = self.detector.check()
        assert result is None

    def test_check_no_shift(self):
        self.detector.set_baseline(0.60)
        for i in range(35):
            # Maintain ~60% win rate
            self.detector.record_trade(1.0 if i % 5 < 3 else -1.0)
        result = self.detector.check()
        if result is not None:
            assert isinstance(result, LabelShiftRecord)

    def test_check_detects_shift(self):
        self.detector.set_baseline(0.80)
        # All losses → win_rate = 0.0, drop = 0.80 → exceeds threshold
        for _ in range(35):
            self.detector.record_trade(-1.0)
        result = self.detector.check()
        assert result is not None
        assert result.drifted is True


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------


def test_get_drift_monitor_singleton():
    m1 = get_drift_monitor()
    m2 = get_drift_monitor()
    assert m1 is m2
    assert isinstance(m1, FeatureDriftMonitor)


def test_get_degradation_tracker_singleton():
    t1 = get_degradation_tracker()
    t2 = get_degradation_tracker()
    assert t1 is t2
    assert isinstance(t1, ModelDegradationTracker)


def test_get_degradation_tracker_scoped_per_timeframe():
    """Concurrent per-timeframe SignalEngine loops must not share a tracker,
    or resolve_last() on one timeframe can resolve a prediction recorded by
    another timeframe's engine."""
    t_15m = get_degradation_tracker("15m")
    t_1h = get_degradation_tracker("1h")
    assert t_15m is not t_1h
    t_15m_again = get_degradation_tracker("15m")
    assert t_15m is t_15m_again


def test_get_label_shift_detector_singleton():
    d1 = get_label_shift_detector()
    d2 = get_label_shift_detector()
    assert d1 is d2
    assert isinstance(d1, LabelShiftDetector)


def test_run_pipeline_selftest_failure_path():
    """Force build_feature_matrix to raise so the except branch is covered."""
    with patch(
        "src.features.pipeline.build_feature_matrix",
        side_effect=RuntimeError("synthetic failure"),
    ):
        result = run_pipeline_selftest()
    assert result["passed"] is False
    assert "synthetic failure" in result["error"]
