"""
Per-strategy kill-switch — v2 Sub-task 3.

Wires the existing PerformanceDriftDetector (src/risk/performance_drift.py)
per strategy_id in the v2 registry, so a strategy whose live performance
drifts materially below its promotion-time baseline is auto-disabled
without touching the other strategies in the portfolio. Re-enabling is
explicit and requires the same out-of-sample validation as initial
promotion — this module never re-enables a strategy on its own.

This is a thin orchestration layer; it does not reimplement drift
detection. All Sharpe/accuracy/win-rate/drawdown comparisons come from
PerformanceDriftDetector.check_drift().

Authority:
  - López de Prado (2018) AFML Ch.11 — backtest overfitting and the need
    for out-of-sample re-validation before reinstating a strategy
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.risk.performance_drift import (
    DriftDetected,
    PerformanceBaseline,
    PerformanceDriftDetector,
)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class StrategyRuntimeState:
    """Tracks one strategy's drift detector and enabled/disabled status."""

    strategy_id: str
    detector: PerformanceDriftDetector
    enabled: bool = True
    disabled_reason: str = ""
    disabled_at_ms: int = 0


class StrategyKillSwitchManager:
    """
    Holds one PerformanceDriftDetector per registered strategy_id and
    exposes an is_enabled() gate the orchestrator/registry must check
    before routing capital to a strategy.
    """

    def __init__(self) -> None:
        self._states: dict[str, StrategyRuntimeState] = {}

    def register_strategy(self, strategy_id: str, baseline: PerformanceBaseline) -> None:
        if strategy_id in self._states:
            raise ValueError(f"strategy_id {strategy_id!r} already has a kill-switch registered")
        self._states[strategy_id] = StrategyRuntimeState(
            strategy_id=strategy_id,
            detector=PerformanceDriftDetector(baseline),
        )
        log.info("strategy_kill_switch.registered", strategy_id=strategy_id)

    def record_trade_outcome(
        self,
        strategy_id: str,
        pnl_usd: float,
        predicted_prob: float,
        actual_direction: int,
        current_equity: float,
        starting_equity: float,
    ) -> None:
        state = self._require_state(strategy_id)
        state.detector.record_trade_outcome(
            pnl_usd=pnl_usd,
            predicted_prob=predicted_prob,
            actual_direction=actual_direction,
            current_equity=current_equity,
            starting_equity=starting_equity,
        )

    def evaluate(self, strategy_id: str, now_ms: int = 0) -> DriftDetected:
        """
        Check the strategy's drift status and auto-disable on detection.

        Once disabled, a strategy stays disabled through repeated evaluate()
        calls until re_enable() is called explicitly — evaluate() never
        flips enabled back to True on its own.
        """
        state = self._require_state(strategy_id)
        drift = state.detector.check_drift()
        if drift.drifted and state.enabled:
            state.enabled = False
            state.disabled_reason = drift.reason
            state.disabled_at_ms = now_ms
            log.warning(
                "strategy_kill_switch.disabled",
                strategy_id=strategy_id,
                reason=drift.reason,
                metric=drift.metric,
            )
        return drift

    def is_enabled(self, strategy_id: str) -> bool:
        return self._require_state(strategy_id).enabled

    def re_enable(self, strategy_id: str) -> None:
        """
        Explicit re-enable after a strategy has passed the same
        out-of-sample promotion gauntlet as initial registration. Callers
        are responsible for that validation — this method does not
        re-validate, it only clears the disabled flag.
        """
        state = self._require_state(strategy_id)
        state.enabled = True
        state.disabled_reason = ""
        state.disabled_at_ms = 0
        log.info("strategy_kill_switch.re_enabled", strategy_id=strategy_id)

    def disabled_reason(self, strategy_id: str) -> str:
        return self._require_state(strategy_id).disabled_reason

    def _require_state(self, strategy_id: str) -> StrategyRuntimeState:
        state = self._states.get(strategy_id)
        if state is None:
            raise KeyError(f"strategy_id {strategy_id!r} has no registered kill-switch")
        return state


_manager: StrategyKillSwitchManager = StrategyKillSwitchManager()


def get_strategy_kill_switch_manager() -> StrategyKillSwitchManager:
    """Module-level singleton for the strategy kill-switch manager."""
    return _manager
