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
from datetime import UTC, datetime

import structlog

from src.config import get_settings
from src.diagnostics.attribution import compute_attribution, get_attribution_tracker
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
from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    GauntletObservation,
    evaluate_gauntlet,
)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MS_PER_DAY: float = 86_400_000.0


class GauntletNotPassedError(RuntimeError):
    """Raised when a re-enable is attempted without clearing the v6 gauntlet."""

    def __init__(self, strategy_id: str, failed_criteria: tuple[str, ...]) -> None:
        self.strategy_id = strategy_id
        self.failed_criteria = failed_criteria
        detail = "; ".join(failed_criteria) or "no attributed track record"
        super().__init__(
            f"strategy {strategy_id!r} has not passed the promotion gauntlet: {detail}"
        )


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

    def build_gauntlet_observation(
        self, strategy_id: str, now_ms: int | None = None
    ) -> GauntletObservation | None:
        """
        Assemble this strategy's live track record for the v6 gauntlet.

        Returns None when the strategy has no attributed fills at all — that
        is an absence of evidence, not evidence of a passing record, and the
        caller must treat it as a failure rather than a zero-valued pass.

        Drawdown is expressed as a fraction of the strategy's own peak
        cumulative P&L, because the gauntlet is specified in percentage terms
        while AttributionTracker reports it in USD. A strategy that never
        reached a positive peak has no meaningful denominator, so it reports
        1.0 and fails the criterion — the right answer to "never made money,
        and we are being asked to give it capital again".
        """
        fills = get_attribution_tracker().fills_for(strategy_id)
        if not fills:
            return None

        attribution = compute_attribution(strategy_id, fills)
        now = now_ms if now_ms is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
        first_entry = min(f.entry_ts for f in fills)
        days_running = max(0.0, (now - first_entry) / _MS_PER_DAY)

        peak_pnl = 0.0
        running = 0.0
        for fill in fills:
            running += fill.pnl_usd
            peak_pnl = max(peak_pnl, running)
        drawdown_pct = attribution.max_drawdown_usd / peak_pnl if peak_pnl > 0.0 else 1.0

        return GauntletObservation(
            trade_count=attribution.trade_count,
            days_running=days_running,
            realized_sharpe=attribution.sharpe,
            realized_max_drawdown_pct=drawdown_pct,
        )

    def re_enable(
        self,
        strategy_id: str,
        *,
        force: bool = False,
        observation: GauntletObservation | None = None,
        criteria: GauntletCriteria | None = None,
        now_ms: int | None = None,
    ) -> None:
        """
        Re-enable a kill-switched strategy after it clears the v6 gauntlet.

        This method used to document that "callers are responsible for that
        validation" and then not validate — and nothing in the tree called
        it, so a strategy auto-disabled for drift could not be reinstated at
        all without a process restart. It now runs
        src/tuning/promotion_gauntlet.py itself, against this strategy's own
        attributed track record, so the discipline is enforced where it is
        stated rather than delegated to a caller that never existed.

        force=True is the explicit operator override, for the case where the
        gauntlet cannot be satisfied from in-process attribution: a restart
        wipes the tracker, so an otherwise-healthy strategy can legitimately
        look like it has no record. It is deliberately loud rather than
        convenient — logged at warning, and reported as an override rather
        than a pass.

        Raises
        ------
        GauntletNotPassedError
            When the gauntlet fails, or when there is no track record to
            evaluate and force was not requested. The strategy stays
            disabled; a failed re-enable never partially applies.
        """
        state = self._require_state(strategy_id)

        if not force:
            checked = (
                observation
                if observation is not None
                else self.build_gauntlet_observation(strategy_id, now_ms=now_ms)
            )
            if checked is None:
                raise GauntletNotPassedError(strategy_id, ())
            result = evaluate_gauntlet(checked, criteria or GauntletCriteria())
            if not result.passed:
                log.warning(
                    "strategy_kill_switch.re_enable_rejected",
                    strategy_id=strategy_id,
                    failed_criteria=list(result.failed_criteria),
                )
                raise GauntletNotPassedError(strategy_id, result.failed_criteria)

        previous_reason = state.disabled_reason
        state.enabled = True
        state.disabled_reason = ""
        state.disabled_at_ms = 0
        log_fn = log.warning if force else log.info
        log_fn(
            "strategy_kill_switch.re_enabled",
            strategy_id=strategy_id,
            gauntlet="forced" if force else "passed",
        )
        # Recorded only on success: a rejected re-enable changed nothing, and
        # logging it as a structural change would put a decision in the audit
        # trail that was never taken.
        _record_structural_change(
            title=f"Strategy {strategy_id} re-enabled",
            change_type="strategy_re_enabled",
            justification=(
                "Operator override — re-enabled WITHOUT passing the v6 promotion "
                "gauntlet. AttributionTracker is in-memory, so a restart can make "
                "a healthy strategy look like it has no record."
                if force
                else "Passed the v6 promotion gauntlet against its own attributed "
                "live track record."
            ),
            evidence={
                "strategy_id": strategy_id,
                "previous_disabled_reason": previous_reason or "(none)",
                "gauntlet": "forced" if force else "passed",
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
