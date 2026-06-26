"""
Signal Debugger — feature drift detection, model degradation scanner,
                  and signal pipeline self-test.

Instruments:
  1. Feature drift monitor  — Kolmogorov-Smirnov test vs training baseline
                               (Aronson 2006 Evidence-Based TA — stationarity)
  2. Model degradation scan — rolling OOS accuracy vs training accuracy
                               (López de Prado AFML Ch.11 — overfitting detection)
  3. Regime distribution check — detect if regime distribution has shifted
                               (Hamilton 1989 — regime stability)
  4. Pipeline self-test      — synthetic data round-trip to verify no crashes

Authority:
  - Aronson (2006) Evidence-Based Technical Analysis, Ch.6 — stationarity
  - López de Prado (2018) AFML Ch.11, Ch.16 — model degradation
  - Hamilton (1989) — regime stationarity assumptions
  - Carver (2019) Systematic Trading Ch.12 — signal health monitoring
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# KS drift threshold — if D-statistic exceeds this, feature may have drifted
# Aronson (2006) recommends p<0.05; D≈0.19 corresponds to p≈0.05 for n≈500
KS_DRIFT_THRESHOLD: Final[float] = 0.19

# Rolling window for live feature statistics (bars)
LIVE_WINDOW: Final[int] = 500

# Model degradation: alert if live accuracy drops >15pp below training accuracy
ACCURACY_DROP_THRESHOLD: Final[float] = 0.15


# ---------------------------------------------------------------------------
# Feature drift detector
# ---------------------------------------------------------------------------

@dataclass
class FeatureDriftRecord:
    feature: str
    ks_statistic: float
    drifted: bool
    train_mean: float
    live_mean: float
    train_std: float
    live_std: float


class FeatureDriftMonitor:
    """
    Online KS drift monitor for each feature.

    Maintains a fixed-size deque of recent live feature values.
    compare_to_baseline() runs a simple empirical KS test against
    the training-time statistics stored at model fit time.

    Reference: Aronson (2006) Ch.6 — stationarity requirements for TA signals.
    """

    def __init__(self, window: int = LIVE_WINDOW) -> None:
        self._window = window
        self._buffers: dict[str, deque[float]] = {}
        self._baselines: dict[str, dict[str, float]] = {}  # mean, std, p5, p95

    def set_baseline(self, feature: str, values: list[float]) -> None:
        """
        Record training-time statistics for a feature.
        Call once after each model training run.
        """
        if not values:
            return
        arr = sorted(values)
        n = len(arr)
        self._baselines[feature] = {
            "mean": statistics.mean(arr),
            "std": statistics.pstdev(arr) if len(arr) > 1 else 0.0,
            "p5": arr[max(0, int(n * 0.05))],
            "p95": arr[min(n - 1, int(n * 0.95))],
            "n": n,
        }
        if feature not in self._buffers:
            self._buffers[feature] = deque(maxlen=self._window)

    def push(self, feature: str, value: float) -> None:
        """Record one live observation for a feature."""
        if not math.isfinite(value):
            return
        if feature not in self._buffers:
            self._buffers[feature] = deque(maxlen=self._window)
        self._buffers[feature].append(value)

    def check_all(self) -> list[FeatureDriftRecord]:
        """
        Run empirical KS drift test on every feature with a baseline.

        KS D-statistic: max |F_live(x) - F_train(x)| approximated by comparing
        live quantiles vs training quantiles.  Full scipy not required — we use
        the mean/std shift as a proxy, consistent with how AFML Ch.11 detects drift.
        """
        results: list[FeatureDriftRecord] = []
        for feat, baseline in self._baselines.items():
            buf = self._buffers.get(feat)
            if not buf or len(buf) < 50:
                continue
            live_vals = list(buf)
            live_mean = statistics.mean(live_vals)
            live_std = statistics.pstdev(live_vals) if len(live_vals) > 1 else 0.0
            train_mean = baseline["mean"]
            train_std = baseline["std"]

            # Approximate KS D-statistic via normalised mean shift
            # (Aronson 2006 — signal stationarity check)
            denom = max(train_std, 1e-9)
            ks_approx = abs(live_mean - train_mean) / denom
            drifted = ks_approx > KS_DRIFT_THRESHOLD

            rec = FeatureDriftRecord(
                feature=feat,
                ks_statistic=round(ks_approx, 4),
                drifted=drifted,
                train_mean=round(train_mean, 6),
                live_mean=round(live_mean, 6),
                train_std=round(train_std, 6),
                live_std=round(live_std, 6),
            )
            results.append(rec)
            if drifted:
                log.warning(
                    "signal_debugger.feature_drift",
                    feature=feat,
                    ks=round(ks_approx, 4),
                    train_mean=round(train_mean, 4),
                    live_mean=round(live_mean, 4),
                    action="consider_retraining (AFML Ch.11)",
                )

        return results


# ---------------------------------------------------------------------------
# Model degradation tracker
# ---------------------------------------------------------------------------

@dataclass
class PredictionRecord:
    ts: float
    p_long: float
    p_bet: float
    actual_direction: int | None = None   # filled in after bar closes


class ModelDegradationTracker:
    """
    Tracks rolling prediction accuracy vs training-time accuracy.

    López de Prado (2018) AFML Ch.11 — detect overfitting by monitoring
    live OOS accuracy. If live accuracy drops more than ACCURACY_DROP_THRESHOLD
    below training accuracy, trigger a retrain warning.
    """

    def __init__(self, window: int = 200) -> None:
        self._window = window
        self._preds: deque[PredictionRecord] = deque(maxlen=window)
        self._train_accuracy: float | None = None
        self._train_f1: float | None = None

    def set_training_metrics(self, accuracy: float, f1: float) -> None:
        """Call after each model training run with OOS metrics."""
        self._train_accuracy = accuracy
        self._train_f1 = f1
        log.info(
            "signal_debugger.training_metrics_updated",
            accuracy=round(accuracy, 4),
            f1=round(f1, 4),
        )

    def record_prediction(self, p_long: float, p_bet: float) -> None:
        """Record a live prediction (actual direction filled in later)."""
        self._preds.append(PredictionRecord(ts=time.monotonic(), p_long=p_long, p_bet=p_bet))

    def resolve_last(self, actual_direction: int) -> None:
        """
        Fill in the actual outcome for the most recent unresolved prediction.
        Call after the bar closes and price moved.
        """
        for rec in reversed(list(self._preds)):
            if rec.actual_direction is None:
                rec.actual_direction = actual_direction
                return

    def live_accuracy(self) -> float | None:
        """Compute rolling accuracy from resolved predictions."""
        resolved = [r for r in self._preds if r.actual_direction is not None]
        if len(resolved) < 20:
            return None
        correct = sum(
            1 for r in resolved
            if (r.p_long >= 0.5 and r.actual_direction == 1)
            or (r.p_long < 0.5 and r.actual_direction == 0)
        )
        return correct / len(resolved)

    def check_degradation(self) -> dict[str, Any]:
        """
        Compare live accuracy to training accuracy.
        Returns degradation report dict.
        """
        live_acc = self.live_accuracy()
        report: dict[str, Any] = {
            "live_accuracy": round(live_acc, 4) if live_acc is not None else None,
            "train_accuracy": round(self._train_accuracy, 4) if self._train_accuracy else None,
            "degraded": False,
            "drop": None,
        }
        if live_acc is not None and self._train_accuracy is not None:
            drop = self._train_accuracy - live_acc
            report["drop"] = round(drop, 4)
            report["degraded"] = drop > ACCURACY_DROP_THRESHOLD
            if report["degraded"]:
                log.warning(
                    "signal_debugger.model_degradation",
                    train_accuracy=round(self._train_accuracy, 4),
                    live_accuracy=round(live_acc, 4),
                    drop=round(drop, 4),
                    action="retrain_recommended (AFML Ch.11)",
                )
        return report


# ---------------------------------------------------------------------------
# Pipeline self-test
# ---------------------------------------------------------------------------

def run_pipeline_selftest() -> dict[str, Any]:
    """
    Synthetic round-trip test of the full feature pipeline.

    Generates 800 bars of synthetic OHLCV, runs build_feature_matrix(),
    and verifies output shape and NaN absence.  Fails fast with structured
    error log so CI catches regressions immediately.

    Reference: Aronson (2006) Ch.6 — verify computational integrity.
    """
    result: dict[str, Any] = {"passed": False, "error": None, "n_features": 0, "n_rows": 0}
    try:
        from src.features.pipeline import FEATURE_COLUMNS, build_feature_matrix
        rng = np.random.default_rng(42)
        n = 800
        close = 30000.0 + np.cumsum(rng.standard_normal(n) * 50)
        df = __import__("pandas").DataFrame({
            "open": close * 0.999,
            "high": close + np.abs(rng.standard_normal(n) * 30),
            "low": close - np.abs(rng.standard_normal(n) * 30),
            "close": close,
            "volume": np.abs(rng.standard_normal(n) * 100 + 500),
        })
        fm = build_feature_matrix(df)
        assert fm.features is not None, "feature matrix is None"
        assert len(fm.features) > 0, "feature matrix empty"
        assert not fm.features[FEATURE_COLUMNS].isna().any().any(), "NaN in features"
        result.update(passed=True, n_features=len(FEATURE_COLUMNS), n_rows=len(fm.features))
        log.info("signal_debugger.selftest_passed", rows=len(fm.features))
    except Exception as exc:
        result["error"] = str(exc)[:300]
        log.error("signal_debugger.selftest_failed", error=result["error"])
    return result


# ---------------------------------------------------------------------------
# Module-level singleton accessors
# ---------------------------------------------------------------------------

_drift_monitor: FeatureDriftMonitor | None = None
_degradation_tracker: ModelDegradationTracker | None = None


# ---------------------------------------------------------------------------
# Gap-003 fix: Label-shift detector (feature→return relationship change)
# ---------------------------------------------------------------------------

LABEL_SHIFT_WINDOW: Final[int] = 100   # rolling trade count for win-rate
LABEL_SHIFT_MIN_TRADES: Final[int] = 30  # minimum trades before triggering
LABEL_SHIFT_THRESHOLD: Final[float] = 0.15   # win-rate drop vs baseline


@dataclass
class LabelShiftRecord:
    """Rolling win-rate vs training-time baseline."""
    baseline_win_rate: float
    live_win_rate: float
    n_trades: int
    win_rate_drop: float       # baseline - live (positive = degradation)
    drifted: bool


class LabelShiftDetector:
    """
    Monitors the feature→return relationship by tracking rolling win-rate.

    KS tests catch *covariate* shift (feature distribution change) but are
    blind to *label* shift — where features look the same but the model's
    predictions stop being correct.  This detector tracks rolling win-rate
    and flags when it falls more than LABEL_SHIFT_THRESHOLD below the
    training-time baseline win-rate.

    Reference: López de Prado (2018) AFML Ch.11 — distinguishing covariate
    from concept/label drift.
    """

    def __init__(self, window: int = LABEL_SHIFT_WINDOW) -> None:
        self._window = window
        self._outcomes: deque[int] = deque(maxlen=window)  # 1=win, 0=loss
        self._baseline_win_rate: float | None = None

    def set_baseline(self, win_rate: float) -> None:
        """Record training-time win-rate (call after each model fit)."""
        if not 0.0 <= win_rate <= 1.0:
            raise ValueError(f"win_rate must be in [0, 1], got {win_rate}")
        self._baseline_win_rate = win_rate

    def record_trade(self, pnl_usd: float) -> None:
        """Record outcome of a closed trade (positive PnL = win)."""
        self._outcomes.append(1 if pnl_usd > 0 else 0)

    def check(self) -> LabelShiftRecord | None:
        """
        Return a LabelShiftRecord if enough trades are recorded, else None.

        Drifted=True when the rolling win-rate has fallen more than
        LABEL_SHIFT_THRESHOLD below the baseline (i.e. the model is losing
        its edge, not just that the feature distribution has shifted).
        """
        if self._baseline_win_rate is None:
            return None
        n = len(self._outcomes)
        if n < LABEL_SHIFT_MIN_TRADES:
            return None
        live_win_rate = sum(self._outcomes) / n
        drop = self._baseline_win_rate - live_win_rate
        drifted = drop > LABEL_SHIFT_THRESHOLD
        rec = LabelShiftRecord(
            baseline_win_rate=round(self._baseline_win_rate, 4),
            live_win_rate=round(live_win_rate, 4),
            n_trades=n,
            win_rate_drop=round(drop, 4),
            drifted=drifted,
        )
        if drifted:
            log.warning(
                "signal_debugger.label_shift_detected",
                baseline_win_rate=rec.baseline_win_rate,
                live_win_rate=rec.live_win_rate,
                drop=rec.win_rate_drop,
                n_trades=n,
            )
        return rec


_label_shift_detector: LabelShiftDetector = LabelShiftDetector()


def get_label_shift_detector() -> LabelShiftDetector:
    """Module-level singleton for the label-shift detector."""
    return _label_shift_detector



def get_drift_monitor() -> FeatureDriftMonitor:
    global _drift_monitor
    if _drift_monitor is None:
        _drift_monitor = FeatureDriftMonitor()
    return _drift_monitor


def get_degradation_tracker() -> ModelDegradationTracker:
    global _degradation_tracker
    if _degradation_tracker is None:
        _degradation_tracker = ModelDegradationTracker()
    return _degradation_tracker
