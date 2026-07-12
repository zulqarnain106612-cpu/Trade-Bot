"""
Shadow-mode tuning runner -- ties proposer + evaluator + gate + store +
audit log together into one attempt cycle.

Design: docs/SELF_TUNING_DESIGN.md §2 (architecture) and §7 (rollout plan,
step 2: "running in shadow mode only -- proposals logged, never promoted").

`shadow_mode=True` (the default) makes this runner behaviorally inert
with respect to real trading: a challenger can clear every gate check and
it will still only be recorded as WOULD_PROMOTE in the audit log, never
written to VersionedConfigStore. Flipping to live promotion
(`SELF_TUNING_SHADOW_MODE=false`) is a separate, explicit operator
configuration step (see rollout plan step 3), not a flag flip here.

When shadow_mode is False and a challenger is promoted, this runner also
advances the parameter's live champion value in ParameterRegistry
(registry.update_current) -- see src/tuning/live_overrides.py, which is
the seam that surfaces that updated value to the live regime/risk/
features/model code paths.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from src.config import SelfTuningSettings
from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.evaluator import EvaluationResult, MetricComparison
from src.tuning.gate import GateDecision, PromotionGate
from src.tuning.proposer import Proposal, TuningProposer
from src.tuning.registry import ParameterRegistry, TunableParameter
from src.tuning.store import VersionedConfigStore


EvaluateFn = Callable[[TunableParameter, Proposal], list[MetricComparison]]


@dataclass(frozen=True)
class AttemptResult:
    param_name: str
    attempted: bool
    accepted: bool
    promoted: bool
    reasons: tuple[str, ...]
    challenger_value: float | None = None


class SelfTuningDisabledError(RuntimeError):
    """Raised only if a caller forces a live (non-shadow) promotion while the kill switch is off."""


class TuningRunner:
    def __init__(
        self,
        registry: ParameterRegistry,
        store: VersionedConfigStore,
        audit_log: TuningAuditLog,
        settings: SelfTuningSettings,
        proposer: TuningProposer,
        gate: PromotionGate,
        shadow_mode: bool = True,
    ) -> None:
        self._registry = registry
        self._store = store
        self._audit_log = audit_log
        self._settings = settings
        self._proposer = proposer
        self._gate = gate
        self._shadow_mode = shadow_mode

    def _cooldown_active(self, param_name: str) -> bool:
        entries = [
            e
            for e in self._audit_log.read_for_param(param_name)
            if e.event_type == TuningEventType.PROPOSED
        ]
        if not entries:
            return False
        last = entries[-1]
        elapsed_hours = (
            datetime.now(UTC) - datetime.fromisoformat(last.timestamp)
        ).total_seconds() / 3600.0
        return elapsed_hours < self._settings.min_hours_between_attempts

    def attempt(
        self,
        param_name: str,
        evaluate_fn: EvaluateFn,
        primary_metric: str,
    ) -> AttemptResult:
        """
        Run one propose -> evaluate -> gate -> (shadow-log | promote) cycle.

        `evaluate_fn(param, proposal) -> list[MetricComparison]` is called
        AFTER the challenger value is proposed, so the caller's backtest
        harness (e.g. run_entropy_threshold_backtest) always evaluates the
        actual proposed value -- not a value chosen independently of what
        the proposer picked. This runner only orchestrates the decision;
        it does not itself run backtests.
        """
        if not self._settings.enabled:
            self._audit_log.record(
                param_name, TuningEventType.SKIPPED, {"reason": "self_tuning_disabled"}
            )
            return AttemptResult(
                param_name=param_name,
                attempted=False,
                accepted=False,
                promoted=False,
                reasons=("self_tuning_disabled",),
            )

        if self._cooldown_active(param_name):
            self._audit_log.record(
                param_name, TuningEventType.SKIPPED, {"reason": "cooldown_active"}
            )
            return AttemptResult(
                param_name=param_name,
                attempted=False,
                accepted=False,
                promoted=False,
                reasons=("cooldown_active",),
            )

        param = self._registry.get(param_name)
        proposal = self._proposer.propose(param)
        self._audit_log.record(
            param_name,
            TuningEventType.PROPOSED,
            {
                "champion_value": proposal.champion_value,
                "challenger_value": proposal.challenger_value,
            },
        )

        comparisons = evaluate_fn(param, proposal)
        evaluation = EvaluationResult(
            param_name=param_name,
            challenger_value=proposal.challenger_value,
            comparisons=tuple(comparisons),
        )
        self._audit_log.record(
            param_name,
            TuningEventType.EVALUATED,
            {
                "comparisons": [
                    {
                        "metric": c.metric_name,
                        "delta": c.delta,
                        "p_value": c.p_value,
                        "significant_improvement": c.significant_improvement,
                        "significant_regression": c.significant_regression,
                    }
                    for c in comparisons
                ]
            },
        )

        decision: GateDecision = self._gate.decide(param, evaluation, primary_metric)

        if not decision.accepted:
            self._audit_log.record(
                param_name, TuningEventType.REJECTED, {"reasons": list(decision.reasons)}
            )
            return AttemptResult(
                param_name=param_name,
                attempted=True,
                accepted=False,
                promoted=False,
                reasons=decision.reasons,
                challenger_value=proposal.challenger_value,
            )

        if self._shadow_mode:
            self._audit_log.record(
                param_name,
                TuningEventType.WOULD_PROMOTE,
                {"challenger_value": proposal.challenger_value, "reasons": list(decision.reasons)},
            )
            return AttemptResult(
                param_name=param_name,
                attempted=True,
                accepted=True,
                promoted=False,
                reasons=decision.reasons,
                challenger_value=proposal.challenger_value,
            )

        self._store.promote(
            param_name,
            proposal.challenger_value,
            evidence={"reasons": list(decision.reasons)},
            promoted_by="bot",
        )
        self._registry.update_current(param_name, proposal.challenger_value)
        self._audit_log.record(
            param_name,
            TuningEventType.PROMOTED,
            {"challenger_value": proposal.challenger_value, "reasons": list(decision.reasons)},
        )
        return AttemptResult(
            param_name=param_name,
            attempted=True,
            accepted=True,
            promoted=True,
            reasons=decision.reasons,
            challenger_value=proposal.challenger_value,
        )
