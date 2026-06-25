"""
Drift Detector Integration — hooks for orchestrator to record trade outcomes.

Adapter layer that wraps executor and signal engine to funnel trade outcomes
into PerformanceDriftDetector.

Usage in orchestrator:
    drift_adapter = DriftIntegrationAdapter(drift_detector, executor)
    await drift_adapter.record_closed_trade(
        trade_id, exit_price, pnl_usd, predicted_prob,
        actual_direction, current_equity, starting_equity
    )
"""

from __future__ import annotations

from typing import Any

import structlog

from src.risk.performance_drift import PerformanceDriftDetector


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class DriftIntegrationAdapter:
    """
    Adapter to funnel trade outcomes from executor → drift detector.
    
    Records P&L, predictions, and equity snapshots after each trade closure.
    """

    def __init__(self, drift_detector: PerformanceDriftDetector | None):
        """Initialize adapter with optional drift detector."""
        self._detector = drift_detector
        self._log = structlog.get_logger(__name__)

    async def record_closed_trade(
        self,
        trade_id: str,
        exit_price: float,
        pnl_usd: float,
        predicted_prob: float,  # Direction model P(long)
        actual_direction: int,  # 1 (long) or -1 (short)
        current_equity: float,
        starting_equity: float,
    ) -> None:
        """
        Record a closed trade outcome to drift detector.
        
        Called by orchestrator after executor.close_position() completes.
        
        Args:
            trade_id: Trade identifier
            exit_price: Close price
            pnl_usd: Trade P&L (can be negative)
            predicted_prob: Direction model prediction [0, 1]
            actual_direction: 1 or -1
            current_equity: Current account equity
            starting_equity: Starting equity for DD calc
        """
        if self._detector is None:
            return
        
        try:
            self._detector.record_trade_outcome(
                pnl_usd=pnl_usd,
                predicted_prob=predicted_prob,
                actual_direction=actual_direction,
                current_equity=current_equity,
                starting_equity=starting_equity,
            )
            
            # Log the trade outcome
            self._log.info(
                "drift.trade_recorded",
                trade_id=trade_id,
                pnl_usd=round(pnl_usd, 2),
                predicted_prob=round(predicted_prob, 3),
                current_equity=round(current_equity, 2),
            )
        except Exception as exc:
            self._log.error(
                "drift.record_failed",
                trade_id=trade_id,
                error=str(exc),
            )

    def check_drift(self) -> dict[str, Any]:
        """
        Check current drift status and return metrics.
        
        Returns:
            dict with keys:
              - drifted (bool): True if drift detected
              - metric (str): which metric drifted (if any)
              - reason (str): human-readable explanation
              - metrics (dict): live performance snapshot
        """
        if self._detector is None:
            return {
                "drifted": False,
                "metric": None,
                "reason": "Drift detector not enabled",
                "metrics": {},
            }
        
        drift = self._detector.check_drift()
        metrics = self._detector.get_live_metrics()
        
        return {
            "drifted": drift.drifted,
            "metric": drift.metric,
            "reason": drift.reason,
            "live_value": drift.live_value,
            "baseline_value": drift.baseline_value,
            "drift_pp": drift.drift_pp,
            "metrics": metrics,
        }
