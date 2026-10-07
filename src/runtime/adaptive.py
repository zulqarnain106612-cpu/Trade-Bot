"""
The adaptive lifecycle: how a tuned parameter, retrained model or generated
strategy becomes live -- and stops being live.

    OBSERVE -> DETECT -> CANDIDATE -> BACKTEST -> STRESS -> RED_TEAM
            -> SHADOW -> CANARY -> PROMOTION_GATE -> (change manager)
            -> PROBATION -> LIVE        or        -> ROLLED_BACK

The existing pieces stay where they are and are connected, not replaced:

* the promotion gate is ``tuning.promotion_gauntlet.evaluate_gauntlet``;
* a model's shadow evidence is ``models.ModelRegistry.evaluate_shadow``;
* probation ends with ``tuning.watchdog.WatchdogOutcome`` -- CLEARED promotes
  the change, ROLLED_BACK rolls it back through the change manager;
* promotion itself is a ``ChangeRequest``, so it still needs a human
  approver whatever produced the candidate.

No proposer validates itself: a stage result recorded by the actor that
proposed the candidate is refused, and only the system (the evaluator that
ran the stage) or a human approver may record one. Stages are recorded in
order and none can be skipped, so a candidate reaches the change manager
only with every stage passed.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from src.runtime.changes import (
    ROLE_APPROVER,
    ROLE_REQUESTER,
    Actor,
    ActorKind,
    ChangeManager,
    ChangeRecord,
    ChangeRequest,
    ChangeStatus,
    UnauthorizedError,
)
from src.runtime.contracts import ChangeClass, ComponentVersion, LifecycleAction
from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    GauntletObservation,
    evaluate_gauntlet,
)
from src.tuning.watchdog import WatchdogOutcome


class AdaptiveStage(StrEnum):
    BACKTEST = "BACKTEST"
    STRESS = "STRESS"
    RED_TEAM = "RED_TEAM"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    PROMOTION_GATE = "PROMOTION_GATE"


STAGE_ORDER: tuple[AdaptiveStage, ...] = tuple(AdaptiveStage)


class CandidateStatus(StrEnum):
    VALIDATING = "VALIDATING"  # stages still to pass
    READY = "READY"  # every stage passed; promotion not yet requested
    PROMOTION_REQUESTED = "PROMOTION_REQUESTED"  # change submitted, awaiting a human
    PROBATION = "PROBATION"  # executed; the watchdog decides
    LIVE = "LIVE"
    REJECTED = "REJECTED"  # a stage or the change request said no
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"  # execution or rollback failed: a person has to look


class AdaptiveError(RuntimeError):
    """The candidate cannot move the way asked."""


@dataclass(frozen=True, slots=True)
class StageEvidence:
    stage: AdaptiveStage
    passed: bool
    detail: str
    evaluator: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    component_id: str
    action: LifecycleAction
    proposer: Actor
    reason: str
    target_version: ComponentVersion | None
    status: CandidateStatus
    evidence: tuple[StageEvidence, ...]
    created_at: datetime
    change_id: str | None = None
    outcome: str | None = None

    @property
    def next_stage(self) -> AdaptiveStage | None:
        done = len(self.evidence)
        return STAGE_ORDER[done] if done < len(STAGE_ORDER) else None

    def to_dict(self) -> dict[str, Any]:
        nxt = self.next_stage
        return {
            "candidate_id": self.candidate_id,
            "component_id": self.component_id,
            "action": self.action.value,
            "proposer": self.proposer.to_dict(),
            "reason": self.reason,
            "target_version": None if self.target_version is None else self.target_version.version,
            "status": self.status.value,
            "next_stage": None if nxt is None else nxt.value,
            "evidence": [
                {
                    "stage": e.stage.value,
                    "passed": e.passed,
                    "detail": e.detail,
                    "evaluator": e.evaluator,
                    "at": e.at.isoformat(),
                }
                for e in self.evidence
            ],
            "change_id": self.change_id,
            "outcome": self.outcome,
            "created_at": self.created_at.isoformat(),
        }


class _ShadowEvaluator(Protocol):
    def evaluate_shadow(self, model_id: str) -> tuple[bool, str]: ...


def shadow_evidence(models: _ShadowEvaluator, model_id: str) -> tuple[bool, str]:
    """A model's SHADOW stage, decided by the model registry's own evaluation."""
    return models.evaluate_shadow(model_id)


def gauntlet_evidence(
    observation: GauntletObservation, criteria: GauntletCriteria | None = None
) -> tuple[bool, str]:
    """The PROMOTION_GATE stage, decided by the existing promotion gauntlet."""
    result = evaluate_gauntlet(observation, criteria or GauntletCriteria())
    return result.passed, "passed" if result.passed else "; ".join(result.failed_criteria)


def _utc_now() -> datetime:
    return datetime.now(UTC)


SYSTEM_EVALUATOR = Actor("adaptive-lifecycle", ActorKind.SYSTEM, frozenset({ROLE_REQUESTER}))

_AFTER_CHANGE: dict[ChangeStatus, CandidateStatus] = {
    ChangeStatus.EXECUTED: CandidateStatus.PROBATION,
    ChangeStatus.PROMOTED: CandidateStatus.LIVE,
    ChangeStatus.ROLLED_BACK: CandidateStatus.ROLLED_BACK,
}


def _after(change: ChangeRecord) -> CandidateStatus:
    return _AFTER_CHANGE.get(change.status, CandidateStatus.FAILED)


class AdaptiveLifecycle:
    def __init__(self, changes: ChangeManager, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._changes = changes
        self._clock = clock
        self._lock = threading.RLock()
        self._candidates: dict[str, Candidate] = {}

    def get(self, candidate_id: str) -> Candidate:
        with self._lock:
            try:
                return self._candidates[candidate_id]
            except KeyError:
                raise AdaptiveError(f"unknown candidate {candidate_id}") from None

    def candidates(self) -> tuple[Candidate, ...]:
        with self._lock:
            return tuple(self._candidates.values())

    def propose(
        self,
        component_id: str,
        action: LifecycleAction,
        proposer: Actor,
        reason: str,
        target_version: ComponentVersion | None = None,
    ) -> Candidate:
        """Any actor -- an optimizer, a trainer, an AI -- may propose."""
        if action not in (LifecycleAction.REPLACE, LifecycleAction.ACTIVATE):
            raise AdaptiveError("a candidate replaces a version or activates a shadow")
        if (action is LifecycleAction.REPLACE) != (target_version is not None):
            raise AdaptiveError("REPLACE needs a target version; ACTIVATE takes none")
        candidate = Candidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
            component_id=component_id,
            action=action,
            proposer=proposer,
            reason=reason,
            target_version=target_version,
            status=CandidateStatus.VALIDATING,
            evidence=(),
            created_at=self._clock(),
        )
        with self._lock:
            self._candidates[candidate.candidate_id] = candidate
        return candidate

    def record(
        self,
        candidate_id: str,
        stage: AdaptiveStage,
        *,
        passed: bool,
        detail: str,
        evaluator: Actor,
    ) -> Candidate:
        with self._lock:
            candidate = self._expect(candidate_id, CandidateStatus.VALIDATING)
            if evaluator.name == candidate.proposer.name:
                raise UnauthorizedError(
                    f"{evaluator.name} proposed {candidate_id}; it cannot judge it"
                )
            if not (
                evaluator.kind is ActorKind.SYSTEM
                or (evaluator.kind is ActorKind.HUMAN and ROLE_APPROVER in evaluator.roles)
            ):
                raise UnauthorizedError(f"{evaluator.name} cannot record {stage.value} results")
            expected = candidate.next_stage
            if stage is not expected:
                wanted = "nothing" if expected is None else expected.value
                raise AdaptiveError(f"{candidate_id} needs {wanted} next, not {stage.value}")
            evidence = (
                *candidate.evidence,
                StageEvidence(stage, passed, detail, evaluator.name, self._clock()),
            )
            if not passed:
                status, outcome = CandidateStatus.REJECTED, f"{stage.value} failed: {detail}"
            elif len(evidence) == len(STAGE_ORDER):
                status, outcome = CandidateStatus.READY, None
            else:
                status, outcome = CandidateStatus.VALIDATING, None
            return self._store(candidate, status=status, evidence=evidence, outcome=outcome)

    def request_promotion(self, candidate_id: str) -> ChangeRecord:
        """
        Submit the change. Every stage has passed, so the change's own
        shadow/canary stage is satisfied from that evidence -- but approval
        remains a human's: a READY candidate never executes by itself.
        """
        with self._lock:
            candidate = self._expect(candidate_id, CandidateStatus.READY)
            change = self._changes.submit(
                ChangeRequest(
                    component_id=candidate.component_id,
                    action=candidate.action,
                    actor=candidate.proposer,
                    reason=f"adaptive {candidate_id}: {candidate.reason}",
                    target_version=candidate.target_version,
                )
            )
            if change.status is ChangeStatus.REJECTED:
                self._store(
                    candidate,
                    status=CandidateStatus.REJECTED,
                    change_id=change.change_id,
                    outcome="change rejected: " + "; ".join(change.reasons),
                )
                return change
            self._store(
                candidate, status=CandidateStatus.PROMOTION_REQUESTED, change_id=change.change_id
            )
            return change

    def on_change_approved(self, candidate_id: str) -> ChangeRecord:
        """After a human approved: record the stage the change class needs, execute."""
        with self._lock:
            candidate = self._expect(candidate_id, CandidateStatus.PROMOTION_REQUESTED)
            change_id = str(candidate.change_id)
            change = self._changes.get(change_id)
            stage = {
                ChangeClass.SHADOW_REQUIRED: AdaptiveStage.SHADOW,
                ChangeClass.CANARY_REQUIRED: AdaptiveStage.CANARY,
            }.get(change.classification)
            if stage is not None and change.status is ChangeStatus.APPROVED:
                passed = next(e for e in candidate.evidence if e.stage is stage)
                self._changes.record_stage(
                    change_id,
                    stage=stage.value.lower(),
                    passed=passed.passed,
                    detail=f"{passed.detail} (recorded by {passed.evaluator})",
                    actor=SYSTEM_EVALUATOR,
                )
            executed = self._changes.execute(change_id, SYSTEM_EVALUATOR)
            status = _after(executed)
            outcome = None
            if status is not CandidateStatus.PROBATION:
                outcome = f"change {executed.status.value}: {executed.result or executed.rollback}"
            self._store(candidate, status=status, outcome=outcome)
            return executed

    def on_watchdog(self, candidate_id: str, outcome: WatchdogOutcome) -> Candidate:
        """Probation ends the way the post-promotion watchdog says."""
        with self._lock:
            candidate = self._expect(candidate_id, CandidateStatus.PROBATION)
            change_id = str(candidate.change_id)
            if outcome is WatchdogOutcome.CLEARED:
                change = self._changes.promote(change_id, SYSTEM_EVALUATOR)
            elif outcome is WatchdogOutcome.ROLLED_BACK:
                change = self._changes.rollback(
                    change_id, SYSTEM_EVALUATOR, "post-promotion watchdog detected drift"
                )
            else:
                return candidate  # still in probation: nothing to decide yet
            return self._store(candidate, status=_after(change), outcome=change.status.value)

    def _expect(self, candidate_id: str, status: CandidateStatus) -> Candidate:
        candidate = self.get(candidate_id)
        if candidate.status is not status:
            raise AdaptiveError(f"{candidate_id} is {candidate.status.value}, not {status.value}")
        return candidate

    def _store(self, candidate: Candidate, **changes: Any) -> Candidate:
        updated = replace(candidate, **changes)
        self._candidates[candidate.candidate_id] = updated
        return updated
