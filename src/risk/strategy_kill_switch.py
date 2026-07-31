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

from collections.abc import Iterable
from dataclasses import dataclass

import structlog

from src.config import get_settings
from src.diagnostics.decision_log_writer import (
    StructuralChangeRecord,
    append_to_decision_log,
)
from src.risk.performance_drift import (
    DriftDetected,
    PerformanceBaseline,
    PerformanceDriftDetector,
)
from src.risk.strategy_decay import CusumDecayDetector


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class StrategyRuntimeState:
    """Tracks one strategy's drift detector and enabled/disabled status."""

    strategy_id: str
    detector: PerformanceDriftDetector
    decay_detector: CusumDecayDetector
    enabled: bool = True
    disabled_reason: str = ""
    disabled_at_ms: int = 0
    # evaluate() runs every tick and the CUSUM statistic stays above its
    # threshold once crossed, so without this the decay entry would be
    # appended on every subsequent tick forever.
    decay_logged: bool = False


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
            # v10 CUSUM decay detector (src/risk/strategy_decay.py) — same
            # oos_sharpe baseline as the drift detector above, but
            # accumulates evidence over time rather than reacting to a
            # single window, so it flags persistent structural decay
            # distinctly from this class's transient drift-triggered halts.
            decay_detector=CusumDecayDetector(baseline_mean=baseline.oos_sharpe),
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

        # v10: feed the same rolling Sharpe the drift check just used into
        # the CUSUM decay detector. current_rolling_sharpe() returns None
        # before the minimum live-trade window fills, matching check_drift's
        # own guard — nothing to accumulate yet in that case.
        rolling_sharpe = state.detector.current_rolling_sharpe()
        if rolling_sharpe is not None:
            state.decay_detector.update(rolling_sharpe)
            if state.decay_detector.is_decayed:
                log.warning(
                    "strategy_kill_switch.structural_decay_flagged",
                    strategy_id=strategy_id,
                    cusum_statistic=round(state.decay_detector.cusum_statistic, 4),
                    reason=(
                        "CUSUM-confirmed structural decay — route to promotion "
                        "gauntlet re-evaluation before any re-enable"
                    ),
                )
                if not state.decay_logged:
                    state.decay_logged = True
                    _record_structural_change(
                        title=f"Strategy {strategy_id} flagged structurally decayed",
                        change_type="strategy_structural_decay",
                        justification=(
                            "CUSUM-confirmed structural decay — route to promotion "
                            "gauntlet re-evaluation before any re-enable"
                        ),
                        evidence={
                            "strategy_id": strategy_id,
                            "cusum_statistic": round(state.decay_detector.cusum_statistic, 4),
                            "rolling_sharpe": round(rolling_sharpe, 4),
                        },
                    )

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
            # Recorded AFTER the state change, never before: a decision log
            # that cannot be written must not stop capital being pulled from
            # a drifting strategy.
            _record_structural_change(
                title=f"Strategy {strategy_id} auto-disabled",
                change_type="strategy_disabled",
                justification=drift.reason,
                evidence={
                    "strategy_id": strategy_id,
                    "metric": drift.metric,
                    "disabled_at_ms": now_ms,
                },
            )
        return drift

    def is_enabled(self, strategy_id: str) -> bool:
        return self._require_state(strategy_id).enabled

    def is_registered(self, strategy_id: str) -> bool:
        """True when this strategy has a kill switch. Never raises."""
        return strategy_id in self._states

    def enabled_ids(self, strategy_ids: Iterable[str]) -> set[str]:
        """
        Subset of ``strategy_ids`` that capital may currently be routed to.

        A strategy with no kill switch registered counts as enabled: absence
        of a switch means no baseline was ever measured, and treating
        "unmeasured" as "disabled" would silently zero the whole portfolio
        the first time this is called before startup finishes registering.
        Being explicitly disabled requires observed drift, not missing data.
        """
        return {
            sid for sid in strategy_ids if not self.is_registered(sid) or self._states[sid].enabled
        }

    def re_enable(self, strategy_id: str) -> None:
        """
        Explicit re-enable after a strategy has passed the same
        out-of-sample promotion gauntlet as initial registration. Callers
        are responsible for that validation — this method does not
        re-validate, it only clears the disabled flag.
        """
        state = self._require_state(strategy_id)
        previous_reason = state.disabled_reason
        state.enabled = True
        state.disabled_reason = ""
        state.disabled_at_ms = 0
        log.info("strategy_kill_switch.re_enabled", strategy_id=strategy_id)
        _record_structural_change(
            title=f"Strategy {strategy_id} re-enabled",
            change_type="strategy_re_enabled",
            justification=(
                "Explicit operator/automation re-enable. This method does not "
                "re-validate; the caller is responsible for having passed the "
                "same out-of-sample gauntlet as initial promotion."
            ),
            evidence={
                "strategy_id": strategy_id,
                "previous_disabled_reason": previous_reason or "(none)",
            },
        )

    def disabled_reason(self, strategy_id: str) -> str:
        return self._require_state(strategy_id).disabled_reason

    def is_structurally_decayed(self, strategy_id: str) -> bool:
        """
        True once the CUSUM detector confirms persistent structural decay
        (v10) — distinct from is_enabled()/disabled_reason(), which track
        v2's transient drift-triggered halts. This flag never disables the
        strategy itself; callers route a decayed strategy to the v6
        promotion gauntlet for full re-evaluation.
        """
        return self._require_state(strategy_id).decay_detector.is_decayed

    def _require_state(self, strategy_id: str) -> StrategyRuntimeState:
        state = self._states.get(strategy_id)
        if state is None:
            raise KeyError(f"strategy_id {strategy_id!r} has no registered kill-switch")
        return state


_manager: StrategyKillSwitchManager = StrategyKillSwitchManager()


def get_strategy_kill_switch_manager() -> StrategyKillSwitchManager:
    """Module-level singleton for the strategy kill-switch manager."""
    return _manager


def _record_structural_change(
    title: str,
    change_type: str,
    justification: str,
    evidence: dict[str, str | float | int],
) -> None:
    """
    Append one structural change to the automated decision log (v10).

    Synchronous file append inside an async application, which is acceptable
    here only because these events are rare by construction — a kill-switch
    trip, a decay flag, an explicit re-enable — not per-tick work.

    Never raises. The log exists so a human can reconstruct why the portfolio
    changed; failing to write it is a loss of auditability, not a reason to
    abandon the risk action that produced it.
    """
    try:
        path = get_settings().storage.decision_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        append_to_decision_log(
            StructuralChangeRecord(
                title=title,
                change_type=change_type,
                justification=justification,
                evidence=evidence,
            ),
            log_path=path,
        )
    except Exception as exc:
        log.error(
            "strategy_kill_switch.decision_log_write_failed",
            change_type=change_type,
            error=str(exc),
            exc_info=True,
        )
