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
from src.tuning.proposer import Proposal, Proposer
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
        proposer: Proposer,
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

    def _cooldown_active(
        self, param_name: str, closed_trade_count: int | None = None
    ) -> tuple[bool, str]:
        """
        Whether this parameter is still inside its post-attempt cooldown.

        Two independent guards, both of which must clear. Wall-clock alone is
        not enough: a quiet market can let 24 hours pass on a handful of
        trades, and re-tuning on that little new evidence is how a tuner
        starts fitting noise. min_trades_between_attempts existed in
        SelfTuningSettings for exactly this and was never read by anything --
        the hours half of the cadence guard was enforced and the trades half
        silently was not.

        closed_trade_count is optional because the runner has no storage
        access of its own. When a caller cannot supply it the trade guard is
        skipped rather than guessed at, and the reason string says so, so a
        cooldown decision is never silently weaker than it appears.
        """
        entries = [
            e
            for e in self._audit_log.read_for_param(param_name)
            if e.event_type == TuningEventType.PROPOSED
        ]
        if not entries:
            return False, ""
        last = entries[-1]
        elapsed_hours = (
            datetime.now(UTC) - datetime.fromisoformat(last.timestamp)
        ).total_seconds() / 3600.0
        if elapsed_hours < self._settings.min_hours_between_attempts:
            return True, "cooldown_active"

        if closed_trade_count is None:
            return False, ""
        previous = last.details.get("closed_trade_count")
        if not isinstance(previous, int):
            # The last attempt predates this field. Nothing to measure
            # against, so the trade guard cannot claim a verdict.
            return False, ""
        if closed_trade_count - previous < self._settings.min_trades_between_attempts:
            return True, "cooldown_active_insufficient_trades"
        return False, ""

    def attempt(
        self,
        param_name: str,
        evaluate_fn: EvaluateFn,
        primary_metric: str,
        closed_trade_count: int | None = None,
    ) -> AttemptResult:
        """
        Run one propose -> evaluate -> gate -> (shadow-log | promote) cycle.

        `evaluate_fn(param, proposal) -> list[MetricComparison]` is called
        AFTER the challenger value is proposed, so the caller's backtest
        harness (e.g. run_entropy_threshold_backtest) always evaluates the
        actual proposed value -- not a value chosen independently of what
        the proposer picked. This runner only orchestrates the decision;
        it does not itself run backtests.

        `closed_trade_count` is the number of closed trades observed so far.
        It feeds the trade half of the cadence guard (see _cooldown_active);
        omitting it leaves only the wall-clock half in force.
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

        blocked, cooldown_reason = self._cooldown_active(param_name, closed_trade_count)
        if blocked:
            self._audit_log.record(param_name, TuningEventType.SKIPPED, {"reason": cooldown_reason})
            return AttemptResult(
                param_name=param_name,
                attempted=False,
                accepted=False,
                promoted=False,
                reasons=(cooldown_reason,),
            )

        param = self._registry.get(param_name)
        proposal = self._proposer.propose(param, primary_metric)
        self._audit_log.record(
            param_name,
            TuningEventType.PROPOSED,
            {
                "champion_value": proposal.champion_value,
                "challenger_value": proposal.challenger_value,
                # Recorded so the NEXT attempt can measure how much new trade
                # evidence has accumulated since this one.
                "closed_trade_count": closed_trade_count,
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
