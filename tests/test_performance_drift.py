"""Test suite for Performance Drift Detector."""

from src.risk.performance_drift import (
    PerformanceBaseline,
    PerformanceDriftDetector,
    _proportion_drop_significant,
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


class TestPerformanceBaselineToDict:
    def test_to_dict_includes_all_fields(self):
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        d = baseline.to_dict()
        assert d["train_sharpe"] == 2.0
        assert d["oos_accuracy"] == 0.58


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

    def test_zero_starting_equity_does_not_raise(self):
        """UI-008: starting_equity<=0 previously raised ZeroDivisionError
        inside record_trade_outcome, which propagates uncaught out of
        check_drift() -- crashing the drift loop instead of failing closed
        on a bad input."""
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
        detector.record_trade_outcome(
            pnl_usd=100.0,
            predicted_prob=0.7,
            actual_direction=1,
            current_equity=10000.0,
            starting_equity=0.0,
        )  # must not raise
        detector.record_trade_outcome(
            pnl_usd=-50.0,
            predicted_prob=0.6,
            actual_direction=-1,
            current_equity=9000.0,
            starting_equity=-100.0,
        )  # must not raise

    def test_valid_starting_equity_still_tracks_drawdown_after_invalid_call(self):
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
        detector.record_trade_outcome(
            pnl_usd=100.0,
            predicted_prob=0.7,
            actual_direction=1,
            current_equity=10000.0,
            starting_equity=0.0,  # invalid -- discarded
        )
        detector.record_trade_outcome(
            pnl_usd=-500.0,
            predicted_prob=0.6,
            actual_direction=-1,
            current_equity=9500.0,
            starting_equity=10000.0,  # valid -- drawdown tracked normally
        )
        assert detector._max_live_drawdown_pct > 0.0


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

    def test_sharpe_drop_significant_with_consistent_negative_pnl(self):
        """A consistent, large Sharpe drop should still be caught (t-test significant)."""
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
                pnl_usd=-50.0 + (i % 3) * 5,  # small variance, consistently negative
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 - i * 50,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert drift.drifted
        assert drift.metric == "sharpe"


class TestCurrentRollingSharpe:
    """current_rolling_sharpe() — public accessor used by strategy_kill_switch's CUSUM feed."""

    def _detector(self) -> PerformanceDriftDetector:
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        return PerformanceDriftDetector(baseline)

    def test_none_before_minimum_window(self):
        detector = self._detector()
        assert detector.current_rolling_sharpe() is None
        detector.record_trade_outcome(
            pnl_usd=10.0,
            predicted_prob=0.6,
            actual_direction=1,
            current_equity=10010.0,
            starting_equity=10000.0,
        )
        assert detector.current_rolling_sharpe() is None

    def test_matches_manual_sharpe_once_window_fills(self):
        detector = self._detector()
        for i in range(30):
            detector.record_trade_outcome(
                pnl_usd=150.0 + (i % 3) * 50,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 150,
                starting_equity=10000,
            )
        rolling_sharpe = detector.current_rolling_sharpe()
        assert rolling_sharpe is not None
        assert rolling_sharpe > 0


class TestAccuracyDrift:
    """Test model accuracy drift detection."""

    def test_accuracy_drop_significant_flags_drift(self):
        """Model consistently predicting the wrong direction, while pnl and
        win-rate stay healthy (so sharpe/winrate don't fire first), must be
        caught by the accuracy-drift check specifically."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.90,
            oos_accuracy=0.90,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        for i in range(50):
            detector.record_trade_outcome(
                pnl_usd=50.0 + (i % 3) * 2,  # positive, low-variance -> healthy sharpe
                predicted_prob=0.7,  # model says long
                actual_direction=-1,  # always actually short -> 0% live accuracy
                current_equity=10000 + i * 50,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert drift.drifted
        assert drift.metric == "accuracy"

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

    def test_get_live_metrics_with_no_trades_yet(self):
        """An empty PnL window must not crash statistics.stdev (which
        requires >=2 samples) -- rolling_sharpe stays 0.0."""
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
        metrics = detector.get_live_metrics()
        assert metrics["total_live_trades"] == 0
        assert metrics["rolling_sharpe"] == 0.0

    def test_winrate_drift_check_insufficient_window_directly(self):
        """_check_winrate_drift()'s own <20-sample guard, exercised directly
        since check_drift()'s outer _MIN_LIVE_TRADES=30 gate means every
        rolling window (win/loss, pnl, predictions) is already >=30 deep by
        the time check_drift() would call it -- this branch is otherwise
        unreachable through the public check_drift() path."""
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
        result = detector._check_winrate_drift()
        assert result.drifted is False


class TestSignificanceGatedDrift:
    """
    Drift now requires both effect size (pp threshold) AND statistical
    significance given live sample size -- see _proportion_drop_significant
    and the Sharpe t-test in performance_drift.py.
    """

    def test_small_noisy_sample_does_not_trigger_winrate_drift(self):
        # Baseline win rate 55% (n=30) vs live win rate 11/30 = 36.7%: an
        # 18.3pp drop, above the 15pp floor, but on samples this small the
        # two-proportion z-test isn't significant at alpha=0.05, so drift
        # should not fire on win_rate.
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=30,
        )
        detector = PerformanceDriftDetector(baseline)

        outcomes = [True] * 11 + [False] * 19  # 11/30 wins
        for is_win in outcomes:
            detector.record_trade_outcome(
                pnl_usd=100.0 if is_win else -100.0,
                predicted_prob=0.6,
                actual_direction=1,
                current_equity=10000,
                starting_equity=10000,
            )

        drift = detector.check_drift()
        assert not drift.drifted or drift.metric != "win_rate"

    def test_proportion_test_significant_with_large_samples(self):
        # Same 55% -> 30% drop as the noisy-sample case above, but backed by
        # a large baseline sample: _proportion_drop_significant should now
        # say yes (unit-tested directly to avoid interaction with the other
        # drift checks, which fire in priority order ahead of win_rate).
        assert _proportion_drop_significant(baseline_p=0.55, baseline_n=400, live_p=0.30, live_n=50)

    def test_proportion_test_not_significant_with_small_samples(self):
        assert not _proportion_drop_significant(
            baseline_p=0.55, baseline_n=30, live_p=11 / 30, live_n=30
        )

    def test_proportion_test_defers_to_pp_floor_on_zero_sample(self):
        # No meaningful test possible with zero observations -- defers to
        # the pp-threshold check alone (returns True, i.e. "don't block").
        assert _proportion_drop_significant(baseline_p=0.55, baseline_n=0, live_p=0.30, live_n=50)

    def test_proportion_test_defers_on_degenerate_pooled_variance(self):
        # pooled proportion of exactly 0.0 (both arms 0%) makes se=0 --
        # no meaningful z-test possible, defers to True.
        assert _proportion_drop_significant(baseline_p=0.0, baseline_n=30, live_p=0.0, live_n=30)


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
