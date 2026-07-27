"""
Post-promotion watchdog -- auto-rollback on live drift.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 5;
docs/SELF_TUNING_DESIGN.md §5.

Wraps the existing, already-audited PerformanceDriftDetector (reused, not
reimplemented) to watch a freshly promoted parameter for a probation
window. Any drift signal within probation triggers an immediate rollback
via VersionedConfigStore.rollback() and locks the parameter out of new
proposals for a cooldown period -- this is the mechanism that makes
"never regress" actually enforceable after promotion, not just before it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from src.config import SelfTuningSettings
from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector
from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.store import VersionedConfigStore


class WatchdogOutcome(StrEnum):
    NOT_IN_PROBATION = "not_in_probation"
    IN_PROBATION = "in_probation"
    CLEARED = "cleared"
    ROLLED_BACK = "rolled_back"
    LOCKED = "locked"


@dataclass
class _ProbationState:
    detector: PerformanceDriftDetector
    started_at: datetime
    trades_recorded: int = 0


class PostPromotionWatchdog:
    def __init__(
        self,
        store: VersionedConfigStore,
        audit_log: TuningAuditLog,
        settings: SelfTuningSettings,
    ) -> None:
        self._store = store
        self._audit_log = audit_log
        self._settings = settings
        self._lock = threading.Lock()
        self._probations: dict[str, _ProbationState] = {}
        self._locked_until: dict[str, datetime] = {}

    def start_probation(self, param_name: str, baseline: PerformanceBaseline) -> None:
        """Call immediately after a promotion (TuningEventType.PROMOTED)."""
        with self._lock:
            self._probations[param_name] = _ProbationState(
                detector=PerformanceDriftDetector(baseline),
                started_at=datetime.now(UTC),
            )

    def is_locked(self, param_name: str) -> bool:
        with self._lock:
            return self._is_locked_unlocked(param_name)

    def _is_locked_unlocked(self, param_name: str) -> bool:
        """Same check as is_locked(), assumes the caller already holds self._lock."""
        until = self._locked_until.get(param_name)
        if until is None:
            return False
        if datetime.now(UTC) >= until:
            del self._locked_until[param_name]
            return False
        return True

    def record_trade_outcome(
        self,
        param_name: str,
        pnl_usd: float,
        predicted_prob: float,
        actual_direction: int,
        current_equity: float,
        starting_equity: float,
    ) -> WatchdogOutcome:
        """
        Feed one closed-trade outcome to the probation detector for
        `param_name`, if it is currently in probation. Returns the
        resulting state -- callers should treat ROLLED_BACK as an
        immediate signal to re-read the champion value from
        VersionedConfigStore before the next decision that depends on it.
        """
        with self._lock:
            state = self._probations.get(param_name)
            if state is None:
                return WatchdogOutcome.NOT_IN_PROBATION

            state.detector.record_trade_outcome(
                pnl_usd, predicted_prob, actual_direction, current_equity, starting_equity
            )
            state.trades_recorded += 1

            drift = state.detector.check_drift()
            if drift.drifted:
                self._store.rollback(param_name)
                self._audit_log.record(
                    param_name,
                    TuningEventType.ROLLED_BACK,
                    {
                        "reason": drift.reason,
                        "metric": drift.metric,
                        "live_value": drift.live_value,
                        "baseline_value": drift.baseline_value,
                    },
                )
                del self._probations[param_name]
                self._locked_until[param_name] = datetime.now(UTC) + timedelta(
                    hours=self._settings.min_hours_between_attempts
                )
                return WatchdogOutcome.ROLLED_BACK

            elapsed_hours = (datetime.now(UTC) - state.started_at).total_seconds() / 3600.0
            if (
                state.trades_recorded >= self._settings.probation_trades
                or elapsed_hours >= self._settings.probation_hours
            ):
                del self._probations[param_name]
                return WatchdogOutcome.CLEARED

            return WatchdogOutcome.IN_PROBATION

    def probation_status(self, param_name: str) -> WatchdogOutcome:
        with self._lock:
            if param_name in self._probations:
                return WatchdogOutcome.IN_PROBATION
            if self._is_locked_unlocked(param_name):
                return WatchdogOutcome.LOCKED
            return WatchdogOutcome.NOT_IN_PROBATION
