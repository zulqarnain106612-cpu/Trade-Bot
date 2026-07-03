"""Test suite for Performance Drift Detector."""

from src.risk.performance_drift import (
    PerformanceBaseline,
    PerformanceDriftDetector,
)


class TestPerformanceBaseline:
    """Test baseline creation and serialization."""

    def test_create_baseline(self):
        """Create a performance baseline."""
        baseline = PerformanceBaseline(
            train_sharpe=2.5,
            oos_sharpe=1.8,
            train_accuracy=0.65,
            oos_accuracy=0.62,
            train_win_rate=0.58,
            max_drawdown_pct=0.12,
            trades_in_backtest=500,
        )
        assert baseline.train_sharpe == 2.5
        assert baseline.oos_sharpe == 1.8
        assert baseline.trades_in_backtest == 500


class TestDriftDetector:
    """Test drift detection across metrics."""

    def test_detector_initialization(self):
        """Initialize drift detector with baseline."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)
        assert detector.baseline == baseline

    def test_insufficient_trades_no_drift(self):
        """Do not detect drift if < 30 trades."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        for i in range(10):
            detector.record_trade_outcome(
                pnl_usd=100.0,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 100,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert not drift.drifted
        assert "Insufficient" in drift.reason


class TestSharpeDrift:
    """Test Sharpe ratio drift detection."""

    def test_sharpe_above_threshold_no_drift(self):
        """Sharpe drop below threshold → no drift."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        for i in range(50):
            detector.record_trade_outcome(
                pnl_usd=150.0 + (i % 3) * 50,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 150,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert not drift.drifted or drift.metric != "sharpe"


class TestAccuracyDrift:
    """Test model accuracy drift detection."""

    def test_accuracy_above_threshold_no_drift(self):
        """Accuracy drop <10pp → no drift."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        for i in range(50):
            is_correct = (i % 100) < 55
            detector.record_trade_outcome(
                pnl_usd=100.0 if is_correct else -50.0,
                predicted_prob=0.7 if is_correct else 0.3,
                actual_direction=1,
                current_equity=10000 + i * 50,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert not drift.drifted or drift.metric != "accuracy"


class TestLiveMetrics:
    """Test live metrics calculation."""

    def test_get_live_metrics(self):
        """Get current live performance metrics."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        for i in range(50):
            detector.record_trade_outcome(
                pnl_usd=100.0,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 100,
                starting_equity=10000,
            )

        metrics = detector.get_live_metrics()
        assert metrics["total_live_trades"] == 50
        assert metrics["total_live_wins"] == 50
        assert metrics["rolling_winrate"] == 1.0
        assert "rolling_sharpe" in metrics
        assert "max_live_drawdown_pct" in metrics


class TestModelDegradationTracker:
    def test_degradation_tracker_flags_low_accuracy_and_sharpe(self):
        from src.diagnostics.signal_debugger import ModelDegradationTracker

        tracker = ModelDegradationTracker(window=50)
        tracker.set_training_metrics(accuracy=0.6, f1=0.55)

        for _ in range(25):
            tracker.record_prediction(p_long=0.4, p_bet=0.5)
            tracker.resolve_last(actual_direction=0)
            tracker.record_trade_result(-10.0)

        report = tracker.check_degradation()
        assert report["degraded"] is True
        assert report["retrain_recommended"] is True
        assert report["tighten_meta_label_threshold"] is True
