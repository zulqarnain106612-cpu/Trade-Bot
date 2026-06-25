"""
Performance Drift Trigger — detects model decay in live trading.

Monitors:
  1. Rolling Sharpe ratio (live window vs training baseline)
  2. Model accuracy degradation (OOS predictions vs training accuracy)
  3. Win rate erosion (live trades vs training backtest)
  4. Max drawdown expansion (live DD vs training max DD)

Halts new positions if any metric exceeds drift threshold.

Authority:
  - López de Prado (2018) AFML Ch.11 — model degradation detection
  - Aronson (2006) Evidence-Based TA Ch.9 — curve-fitting and overfitting
  - Carver (2019) Systematic Trading Ch.12 — signal health monitoring
  - Bailey et al. (2014) "The Deflated Sharpe Ratio" — performance expectations
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Drift thresholds: halt new positions if exceeded
_DRIFT_SHARPE_DROP_PP: Final[float] = 0.5  # If live Sharpe drops >0.5pp vs training, halt
_DRIFT_ACCURACY_DROP_PP: Final[float] = 0.10  # >10pp drop in model accuracy
_DRIFT_WINRATE_DROP_PP: Final[float] = 0.15  # >15pp drop in win rate
_DRIFT_DRAWDOWN_EXPAND_PP: Final[float] = 0.10  # Max DD expands >10pp vs training max DD

# Rolling window for live performance calculation (trades)
_LIVE_WINDOW_TRADES: Final[int] = 50  # Calculate Sharpe over last 50 trades

# Minimum trades required before drift detection (avoid false alarms early)
_MIN_LIVE_TRADES: Final[int] = 30


@dataclass
class PerformanceBaseline:
    """
    Training-time performance baseline — set once during model training.
    
    Attributes:
        train_sharpe: In-sample Sharpe ratio from backtest
        oos_sharpe: Out-of-sample Sharpe ratio (walk-forward validation)
        train_accuracy: Training set model accuracy (%)
        oos_accuracy: Out-of-sample accuracy (%)
        train_win_rate: Training set win rate (%)
        max_drawdown_pct: Maximum drawdown from backtest (%)
        trades_in_backtest: Total trades in backtest
        set_at_ms: UNIX timestamp when baseline was recorded
    """
    train_sharpe: float
    oos_sharpe: float
    train_accuracy: float
    oos_accuracy: float
    train_win_rate: float
    max_drawdown_pct: float
    trades_in_backtest: int
    set_at_ms: int = field(default_factory=lambda: int(datetime.now(tz=UTC).timestamp() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_sharpe": self.train_sharpe,
            "oos_sharpe": self.oos_sharpe,
            "train_accuracy": self.train_accuracy,
            "oos_accuracy": self.oos_accuracy,
            "train_win_rate": self.train_win_rate,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trades_in_backtest": self.trades_in_backtest,
            "set_at_ms": self.set_at_ms,
        }


@dataclass
class DriftDetected:
    """Drift detection result — what drifted and by how much."""
    drifted: bool
    reason: str = ""
    metric: str = ""  # 'sharpe' | 'accuracy' | 'win_rate' | 'drawdown'
    live_value: float = 0.0
    baseline_value: float = 0.0
    drift_pp: float = 0.0  # Percentage points of drift


class PerformanceDriftDetector:
    """
    Monitors live trading performance vs training baseline.
    
    Maintains rolling window of live P&L and trade results.
    Detects degradation in Sharpe, accuracy, win rate, max drawdown.
    """

    def __init__(self, baseline: PerformanceBaseline):
        """Initialize with training baseline."""
        self._baseline = baseline
        self._log = structlog.get_logger(__name__)

        # Rolling window of live P&L (in USD)
        self._live_pnl_window: deque[float] = deque(maxlen=_LIVE_WINDOW_TRADES)

        # Rolling window of trade outcomes: True=win, False=loss
        self._live_wins_window: deque[bool] = deque(maxlen=_LIVE_WINDOW_TRADES)

        # Rolling window of model predictions vs actual
        self._live_predictions: deque[tuple[float, int]] = deque(maxlen=_LIVE_WINDOW_TRADES)
        # Each element: (predicted_prob, actual_direction)

        # Overall live trading stats
        self._total_live_trades: int = 0
        self._total_live_wins: int = 0
        self._max_live_drawdown_pct: float = 0.0
        self._live_equity_peak: float = 0.0
        self._live_equity_start: float = 0.0

    @property
    def baseline(self) -> PerformanceBaseline:
        """Return training baseline (immutable)."""
        return self._baseline

    def record_trade_outcome(
        self,
        pnl_usd: float,
        predicted_prob: float,
        actual_direction: int,
        current_equity: float,
        starting_equity: float,
    ) -> None:
        """
        Record a live trade outcome and update rolling stats.
        
        Args:
            pnl_usd: Trade P&L in USD (can be negative)
            predicted_prob: Model prediction probability [0, 1]
            actual_direction: Actual direction: 1 (long) or -1 (short)
            current_equity: Current account equity
            starting_equity: Starting equity for drawdown calc
        """
        # Record P&L to rolling window
        self._live_pnl_window.append(pnl_usd)

        # Record win/loss (win if pnl > 0)
        is_win = pnl_usd > 0
        self._live_wins_window.append(is_win)

        # Record prediction accuracy
        # Model direction: 1 if prob > 0.5 else -1
        model_direction = 1 if predicted_prob > 0.5 else -1
        is_correct = (model_direction == actual_direction)
        self._live_predictions.append((predicted_prob, 1 if is_correct else 0))

        # Update cumulative stats
        self._total_live_trades += 1
        if is_win:
            self._total_live_wins += 1

        # Update drawdown tracking
        if current_equity > self._live_equity_peak:
            self._live_equity_peak = current_equity

        if self._live_equity_start == 0:
            self._live_equity_start = starting_equity

        drawdown_pct = (self._live_equity_peak - current_equity) / starting_equity
        if drawdown_pct > self._max_live_drawdown_pct:
            self._max_live_drawdown_pct = drawdown_pct

    def check_drift(self) -> DriftDetected:
        """
        Check if performance has drifted beyond acceptable thresholds.
        
        Returns:
            DriftDetected with details on what drifted (if anything)
        """
        # Need minimum live trades to avoid false alarms
        if self._total_live_trades < _MIN_LIVE_TRADES:
            return DriftDetected(
                drifted=False,
                reason=f"Insufficient live trades ({self._total_live_trades} < {_MIN_LIVE_TRADES})",
            )

        # Check each metric for drift
        drift_checks = [
            self._check_sharpe_drift(),
            self._check_accuracy_drift(),
            self._check_winrate_drift(),
            self._check_drawdown_drift(),
        ]

        for drift in drift_checks:
            if drift.drifted:
                self._log.warning(
                    "performance_drift_detected",
                    metric=drift.metric,
                    live_value=round(drift.live_value, 4),
                    baseline=round(drift.baseline_value, 4),
                    drift_pp=round(drift.drift_pp, 3),
                )
                return drift

        return DriftDetected(drifted=False, reason="All metrics within drift thresholds")

    def _check_sharpe_drift(self) -> DriftDetected:
        """Check if rolling Sharpe has dropped >0.5pp vs training OOS Sharpe."""
        if len(self._live_pnl_window) < 20:
            return DriftDetected(drifted=False)

        # Calculate rolling Sharpe
        pnl_list = list(self._live_pnl_window)
        mean_pnl = statistics.mean(pnl_list)

        if len(pnl_list) < 2:
            return DriftDetected(drifted=False)

        std_pnl = statistics.stdev(pnl_list)

        # Sharpe = mean / std (annualization skipped for rolling window)
        if std_pnl > 0:
            live_sharpe = mean_pnl / std_pnl
        else:
            # No variance (all same P&L) — assign 0 Sharpe
            # This triggers drift detection if baseline > 0
            live_sharpe = 0.0 if mean_pnl == 0 else (mean_pnl * 10)  # Proxy for zero-variance case

        baseline_sharpe = self._baseline.oos_sharpe
        drift_pp = baseline_sharpe - live_sharpe

        if drift_pp > _DRIFT_SHARPE_DROP_PP:
            return DriftDetected(
                drifted=True,
                metric="sharpe",
                live_value=live_sharpe,
                baseline_value=baseline_sharpe,
                drift_pp=drift_pp,
                reason=f"Sharpe drifted {drift_pp:.3f}pp below baseline ({live_sharpe:.2f} vs {baseline_sharpe:.2f})",
            )

        return DriftDetected(drifted=False)

    def _check_accuracy_drift(self) -> DriftDetected:
        """Check if model accuracy has dropped >10pp vs OOS baseline."""
        if len(self._live_predictions) < 20:
            return DriftDetected(drifted=False)

        # Calculate live accuracy from predictions
        if not self._live_predictions:
            return DriftDetected(drifted=False)

        correct = sum(1 for _, result in self._live_predictions if result == 1)
        live_accuracy = correct / len(self._live_predictions)

        baseline_accuracy = self._baseline.oos_accuracy
        drift_pp = baseline_accuracy - live_accuracy

        if drift_pp > _DRIFT_ACCURACY_DROP_PP:
            return DriftDetected(
                drifted=True,
                metric="accuracy",
                live_value=live_accuracy,
                baseline_value=baseline_accuracy,
                drift_pp=drift_pp,
                reason=f"Accuracy drifted {drift_pp:.1%} below baseline ({live_accuracy:.1%} vs {baseline_accuracy:.1%})",
            )

        return DriftDetected(drifted=False)

    def _check_winrate_drift(self) -> DriftDetected:
        """Check if win rate has dropped >15pp vs training baseline."""
        if len(self._live_wins_window) < 20:
            return DriftDetected(drifted=False)

        live_wins = sum(1 for w in self._live_wins_window if w)
        live_winrate = live_wins / len(self._live_wins_window)

        baseline_winrate = self._baseline.train_win_rate
        drift_pp = baseline_winrate - live_winrate

        if drift_pp > _DRIFT_WINRATE_DROP_PP:
            return DriftDetected(
                drifted=True,
                metric="win_rate",
                live_value=live_winrate,
                baseline_value=baseline_winrate,
                drift_pp=drift_pp,
                reason=f"Win rate drifted {drift_pp:.1%} below baseline ({live_winrate:.1%} vs {baseline_winrate:.1%})",
            )

        return DriftDetected(drifted=False)

    def _check_drawdown_drift(self) -> DriftDetected:
        """Check if max drawdown has expanded >10pp vs training max DD."""
        baseline_dd = self._baseline.max_drawdown_pct
        live_dd = self._max_live_drawdown_pct
        drift_pp = live_dd - baseline_dd

        if drift_pp > _DRIFT_DRAWDOWN_EXPAND_PP:
            return DriftDetected(
                drifted=True,
                metric="drawdown",
                live_value=live_dd,
                baseline_value=baseline_dd,
                drift_pp=drift_pp,
                reason=f"Max drawdown expanded {drift_pp:.1%} beyond baseline ({live_dd:.1%} vs {baseline_dd:.1%})",
            )

        return DriftDetected(drifted=False)

    def get_live_metrics(self) -> dict[str, Any]:
        """Return current live performance metrics."""
        live_sharpe = 0.0
        if len(self._live_pnl_window) > 1:
            mean_pnl = statistics.mean(list(self._live_pnl_window))
            std_pnl = statistics.stdev(list(self._live_pnl_window))
            live_sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0

        live_winrate = (
            sum(1 for w in self._live_wins_window if w) / len(self._live_wins_window)
            if self._live_wins_window
            else 0.0
        )

        live_accuracy = (
            sum(1 for _, r in self._live_predictions if r == 1) / len(self._live_predictions)
            if self._live_predictions
            else 0.0
        )

        return {
            "total_live_trades": self._total_live_trades,
            "total_live_wins": self._total_live_wins,
            "rolling_sharpe": round(live_sharpe, 3),
            "rolling_winrate": round(live_winrate, 3),
            "rolling_accuracy": round(live_accuracy, 3),
            "max_live_drawdown_pct": round(self._max_live_drawdown_pct, 4),
            "rolling_window_size": len(self._live_pnl_window),
        }
