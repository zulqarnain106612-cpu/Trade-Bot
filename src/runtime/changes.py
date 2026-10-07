"""
The change manager -- the only path that mutates runtime state.

    REQUEST -> CLASSIFY + DEPENDENCY ANALYSIS -> VALIDATE -> POLICY
            -> (APPROVAL) -> PREPARE -> (SHADOW / CANARY STAGE) -> ACTIVATE
            -> OBSERVE -> PROMOTE  or  ROLLBACK

Every request names its actor, reason, component, action and the version the
requester saw. Every status change is appended to the audit log (and handed
to the audit sinks) before the call returns.

Policy:

* FORBIDDEN, DRAIN_REQUIRED and RESTART_REQUIRED changes are rejected with
  the reason (the latter two name the step that has to come first).
* LIVE_SAFE changes need only an authorised requester: taking risk off must
  never wait for a person.
* Everything else waits for approval by a human holding the approver role.
  An AI actor can request but never approve, and never approves its own
  change by being the requester.
* SHADOW_REQUIRED and CANARY_REQUIRED changes execute only once a passed
  stage result is recorded for them.
* One open change per component: a second request is rejected, so two
  operators (or an operator and the reconciler) cannot interleave.
* A request carrying ``expected_version`` is rejected when the component has
  moved on (stale version).
* After execution the supervisor probes the component; an unhealthy result
  rolls the change back where an inverse action exists.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from src.runtime.contracts import (
    DESIRABLE_STATES,
    ChangeClass,
    ComponentVersion,
    DesiredState,
    HealthState,
    InvalidTransitionError,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    UnsupportedActionError,
)
from src.runtime.dependencies import DependencyGraph, ImpactReport
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import SupervisorError, SupervisorSet

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

A = LifecycleAction


class ActorKind(StrEnum):
    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    name: str
    kind: ActorKind
    roles: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind.value, "roles": sorted(self.roles)}


ROLE_REQUESTER = "runtime.request"
ROLE_APPROVER = "runtime.approve"


class ChangeStatus(StrEnum):
    REJECTED = "REJECTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    STAGED = "STAGED"  # required shadow/canary stage passed
    EXECUTED = "EXECUTED"  # activated, under observation
    PROMOTED = "PROMOTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    CANCELLED = "CANCELLED"


TERMINAL: frozenset[ChangeStatus] = frozenset(
    {
        ChangeStatus.REJECTED,
        ChangeStatus.PROMOTED,
        ChangeStatus.FAILED,
        ChangeStatus.ROLLED_BACK,
        ChangeStatus.ROLLBACK_FAILED,
        ChangeStatus.CANCELLED,
    }
)

STAGE_FOR: dict[ChangeClass, str] = {
    ChangeClass.SHADOW_REQUIRED: "shadow",
    ChangeClass.CANARY_REQUIRED: "canary",
}

REJECTED_CLASSES: frozenset[ChangeClass] = frozenset(
    {ChangeClass.FORBIDDEN, ChangeClass.DRAIN_REQUIRED, ChangeClass.RESTART_REQUIRED}
)

# The action that undoes each action, where one exists.
INVERSE: dict[LifecycleAction, LifecycleAction] = {
    A.ACTIVATE: A.DEACTIVATE,
    A.DEACTIVATE: A.ACTIVATE,
    A.PAUSE: A.RESUME,
    A.RESUME: A.PAUSE,
    A.START: A.STOP,
    A.REPLACE: A.ROLLBACK,
}


class ChangeError(RuntimeError):
    """The request cannot move in the way asked."""


class UnauthorizedError(ChangeError):
    """The actor lacks the role for this step."""


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    component_id: str
    action: LifecycleAction
    actor: Actor
    reason: str
    target_version: ComponentVersion | None = None
    expected_version: str | None = None
    change_id: str = field(default_factory=lambda: f"chg-{uuid.uuid4().hex[:12]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "component_id": self.component_id,
            "action": self.action.value,
            "actor": self.actor.to_dict(),
            "reason": self.reason,
            "target_version": None if self.target_version is None else self.target_version.version,
            "expected_version": self.expected_version,
        }


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: str
    passed: bool
    detail: str
    recorded_by: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    request: ChangeRequest
    status: ChangeStatus
    classification: ChangeClass
    reasons: tuple[str, ...]
    impact: ImpactReport | None
    old_version: str | None
    new_version: str | None
    created_at: datetime
    updated_at: datetime
    approved_by: str | None = None
    stages: tuple[StageResult, ...] = ()
    result: str | None = None
    rollback: str | None = None

    @property
    def change_id(self) -> str:
        return self.request.change_id

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.request.to_dict(),
            "status": self.status.value,
            "classification": self.classification.value,
            "reasons": list(self.reasons),
            "impact": None if self.impact is None else self.impact.to_dict(),
            "old_version": self.old_version,
            "new_version": self.new_version,
            "approved_by": self.approved_by,
            "stages": [
                {"stage": s.stage, "passed": s.passed, "detail": s.detail, "by": s.recorded_by}
                for s in self.stages
            ],
            "result": self.result,
            "rollback": self.rollback,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AuditEntry:
    change_id: str
    component_id: str
    event: str
    actor: str
    detail: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "component_id": self.component_id,
            "event": self.event,
            "actor": self.actor,
            "detail": self.detail,
            "at": self.at.isoformat(),
        }


AuditSink = Callable[[AuditEntry], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ChangeManager:
    def __init__(
        self,
        registry: RuntimeRegistry,
        supervisors: SupervisorSet,
        *,
        clock: Callable[[], datetime] = _utc_now,
        audit_sinks: Iterable[AuditSink] = (),
    ) -> None:
        self._registry = registry
        self._supervisors = supervisors
        self._clock = clock
        self._sinks = list(audit_sinks)
        self._lock = threading.RLock()
        self._changes: dict[str, ChangeRecord] = {}
        self._audit: list[AuditEntry] = []
        # Desired state before each executed change, restored on rollback.
        self._previous_desired: dict[str, DesiredState | None] = {}

    # -- queries -------------------------------------------------------

    def get(self, change_id: str) -> ChangeRecord:
        with self._lock:
            try:
                return self._changes[change_id]
            except KeyError:
                raise ChangeError(f"unknown change {change_id}") from None

    def changes(self, component_id: str | None = None) -> tuple[ChangeRecord, ...]:
        with self._lock:
            return tuple(
                c
                for c in self._changes.values()
                if component_id is None or c.request.component_id == component_id
            )

    def audit_log(self, change_id: str | None = None) -> tuple[AuditEntry, ...]:
        with self._lock:
            return tuple(e for e in self._audit if change_id is None or e.change_id == change_id)

    def open_change(self, component_id: str) -> ChangeRecord | None:
        with self._lock:
            for c in self._changes.values():
                if c.request.component_id == component_id and c.status not in TERMINAL:
                    return c
            return None

    # -- the flow ------------------------------------------------------

    def submit(self, request: ChangeRequest) -> ChangeRecord:
        """Classify, analyse, validate and apply policy. Never executes."""
        with self._lock:
            existing = self._changes.get(request.change_id)
            if existing is not None:
                if existing.request == request:
                    return existing  # an idempotent resubmission
                raise ChangeError(f"change id {request.change_id} already names another request")
            now = self._clock()
            record = self._registry.find(request.component_id)
            if record is None:
                return self._new(
                    request,
                    ChangeStatus.REJECTED,
                    ChangeClass.FORBIDDEN,
                    (f"unknown component {request.component_id}",),
                    None,
                    now,
                    old_version=None,
                    new_version=None,
                )
            old_version = record.version.version
            reject = self._precheck(request, old_version)
            if reject is not None:
                return self._new(
                    request,
                    ChangeStatus.REJECTED,
                    ChangeClass.FORBIDDEN,
                    (reject,),
                    None,
                    now,
                    old_version=old_version,
                    new_version=None,
                )
            impact = DependencyGraph.from_registry(self._registry).impact(
                request.component_id, request.action, request.target_version
            )
            if impact.rollout_mode in REJECTED_CLASSES:
                return self._new(
                    request,
                    ChangeStatus.REJECTED,
                    impact.rollout_mode,
                    impact.reasons,
                    impact,
                    now,
                    old_version=old_version,
                    new_version=None,
                )
            try:
                _, version = self._registry.preview(
                    request.component_id, request.action, request.target_version
                )
            except (RuntimeContractError, InvalidTransitionError, UnsupportedActionError) as exc:
                return self._new(
                    request,
                    ChangeStatus.REJECTED,
                    impact.rollout_mode,
                    (str(exc),),
                    impact,
                    now,
                    old_version=old_version,
                    new_version=None,
                )
            status = (
                ChangeStatus.APPROVED
                if impact.rollout_mode is ChangeClass.LIVE_SAFE
                else ChangeStatus.AWAITING_APPROVAL
            )
            return self._new(
                request,
                status,
                impact.rollout_mode,
                impact.reasons,
                impact,
                now,
                old_version=old_version,
                new_version=version.version,
            )

    def _precheck(self, request: ChangeRequest, current_version: str) -> str | None:
        if ROLE_REQUESTER not in request.actor.roles:
            return f"{request.actor.name} lacks {ROLE_REQUESTER}"
        if not request.reason.strip():
            return "a change needs a reason"
        if request.expected_version is not None and request.expected_version != current_version:
            return (
                f"stale: requested against {request.expected_version}, "
                f"component runs {current_version}"
            )
        open_change = self.open_change(request.component_id)
        if open_change is not None:
            return f"{open_change.change_id} is still open on {request.component_id}"
        return None

    def request_desired(
        self,
        component_id: str,
        target_state: LifecycleState,
        actor: Actor,
        reason: str,
        target_version: str | None = None,
    ) -> DesiredState:
        """
        Record intent. Nothing runs here: the reconciler turns the gap into
        change requests, so risk-reducing steps execute at once and every
        other step still waits for a human approver.
        """
        with self._lock:
            if ROLE_REQUESTER not in actor.roles:
                raise UnauthorizedError(f"{actor.name} lacks {ROLE_REQUESTER}")
            desired = DesiredState(
                target_state=target_state,
                requested_by=actor.name,
                reason=reason,
                requested_at=self._clock(),
                target_version=target_version,
            )
            self._registry.set_desired(component_id, desired)
            self._append_audit(
                f"desired:{component_id}",
                component_id,
                "desired_set",
                actor.name,
                f"{target_state.value}@{target_version or 'current'}: {reason}",
            )
            return desired

    def approve(self, change_id: str, approver: Actor) -> ChangeRecord:
        with self._lock:
            change = self._expect(change_id, ChangeStatus.AWAITING_APPROVAL)
            if approver.kind is not ActorKind.HUMAN or ROLE_APPROVER not in approver.roles:
                self._audit_entry(change, "approval_refused", approver.name, approver.kind.value)
                raise UnauthorizedError(
                    f"{approver.name} ({approver.kind.value}) cannot approve: "
                    f"a human with {ROLE_APPROVER} must"
                )
            return self._move(
                change, ChangeStatus.APPROVED, approver.name, "approved", approved_by=approver.name
            )

    def record_stage(
        self, change_id: str, *, stage: str, passed: bool, detail: str, actor: Actor
    ) -> ChangeRecord:
        """
        Record a shadow/canary outcome. Only a human approver or the system
        (the evaluator that ran the stage) may: a stage result from an AI
        actor would let it promote its own candidate.
        """
        with self._lock:
            change = self._expect(change_id, ChangeStatus.APPROVED)
            required = STAGE_FOR.get(change.classification)
            if required is None or stage != required:
                raise ChangeError(
                    f"{change_id} ({change.classification.value}) needs no {stage} stage"
                )
            if not (
                actor.kind is ActorKind.SYSTEM
                or (actor.kind is ActorKind.HUMAN and ROLE_APPROVER in actor.roles)
            ):
                self._audit_entry(change, "stage_refused", actor.name, stage)
                raise UnauthorizedError(f"{actor.name} cannot record a {stage} result")
            result = StageResult(stage, passed, detail, actor.name, self._clock())
            stages = (*change.stages, result)
            if passed:
                return self._move(
                    change, ChangeStatus.STAGED, actor.name, f"{stage}_passed", stages=stages
                )
            return self._move(
                change,
                ChangeStatus.FAILED,
                actor.name,
                f"{stage}_failed",
                stages=stages,
                result=f"{stage} stage failed: {detail}",
            )

    def execute(self, change_id: str, actor: Actor) -> ChangeRecord:
        with self._lock:
            change = self.get(change_id)
            if ROLE_REQUESTER not in actor.roles:
                raise UnauthorizedError(f"{actor.name} lacks {ROLE_REQUESTER}")
            needs_stage = change.classification in STAGE_FOR
            ready = ChangeStatus.STAGED if needs_stage else ChangeStatus.APPROVED
            if change.status is not ready:
                stage = STAGE_FOR.get(change.classification)
                hint = "" if stage is None else f" (a passed {stage} stage)"
                raise ChangeError(
                    f"{change_id} is {change.status.value}; execution needs {ready.value}{hint}"
                )
            request = change.request
            current = self._registry.get(request.component_id)
            if current.version.version != change.old_version:
                return self._move(
                    change,
                    ChangeStatus.FAILED,
                    actor.name,
                    "stale",
                    result=f"component moved to {current.version.version} after approval",
                )
            previous_desired = current.desired
            try:
                transition = self._supervisors.for_component(request.component_id).execute(
                    request.component_id,
                    request.action,
                    actor=actor.name,
                    target_version=request.target_version,
                    change_id=change_id,
                )
            except (
                SupervisorError,
                RuntimeContractError,
                InvalidTransitionError,
                UnsupportedActionError,
            ) as exc:
                return self._move(
                    change, ChangeStatus.FAILED, actor.name, "execution_failed", result=str(exc)
                )
            if transition.to_state in DESIRABLE_STATES:
                # Setup steps (VALIDATE, INITIALIZE, a cold swap) are not targets.
                self._registry.set_desired(
                    request.component_id,
                    DesiredState(
                        target_state=transition.to_state,
                        requested_by=request.actor.name,
                        reason=request.reason,
                        requested_at=self._clock(),
                        target_version=transition.to_version,
                    ),
                )
            executed = self._move(
                change,
                ChangeStatus.EXECUTED,
                actor.name,
                "executed",
                result=f"{transition.from_state} -> {transition.to_state.value}",
            )
            self._previous_desired[change_id] = previous_desired
            final = change.classification is ChangeClass.LIVE_SAFE
            return self._observe(executed, actor, final=final)

    def promote(self, change_id: str, actor: Actor) -> ChangeRecord:
        """End observation: PROMOTED when healthy, rolled back when not."""
        with self._lock:
            change = self._expect(change_id, ChangeStatus.EXECUTED)
            if not (
                actor.kind is ActorKind.SYSTEM
                or (actor.kind is ActorKind.HUMAN and ROLE_APPROVER in actor.roles)
            ):
                raise UnauthorizedError(f"{actor.name} cannot promote")
            return self._observe(change, actor, final=True)

    def rollback(self, change_id: str, actor: Actor, reason: str) -> ChangeRecord:
        with self._lock:
            change = self._expect(change_id, ChangeStatus.EXECUTED)
            if ROLE_REQUESTER not in actor.roles:
                raise UnauthorizedError(f"{actor.name} lacks {ROLE_REQUESTER}")
            return self._rollback(change, actor, reason)

    def cancel(self, change_id: str, actor: Actor) -> ChangeRecord:
        with self._lock:
            change = self.get(change_id)
            if change.status in TERMINAL or change.status is ChangeStatus.EXECUTED:
                raise ChangeError(f"{change_id} is {change.status.value}; nothing to cancel")
            if actor.name != change.request.actor.name and ROLE_APPROVER not in actor.roles:
                raise UnauthorizedError(f"{actor.name} cannot cancel {change_id}")
            return self._move(change, ChangeStatus.CANCELLED, actor.name, "cancelled")

    # -- internals -----------------------------------------------------

    def _observe(self, change: ChangeRecord, actor: Actor, *, final: bool) -> ChangeRecord:
        component_id = change.request.component_id
        report = self._supervisors.for_component(component_id).probe(component_id)
        if report.state is HealthState.UNHEALTHY:
            return self._rollback(change, actor, f"unhealthy after change: {report.detail}")
        if not final:
            self._audit_entry(change, "observing", actor.name, report.state.value)
            return change
        return self._move(
            change, ChangeStatus.PROMOTED, actor.name, "promoted", result=report.state.value
        )

    def _rollback(self, change: ChangeRecord, actor: Actor, reason: str) -> ChangeRecord:
        request = change.request
        inverse = INVERSE.get(request.action)
        record = self._registry.get(request.component_id)
        if inverse is None or inverse not in record.spec.capabilities.actions:
            return self._move(
                change,
                ChangeStatus.ROLLBACK_FAILED,
                actor.name,
                "rollback_unavailable",
                rollback=f"{reason}; no supported inverse of {request.action.value}",
            )
        try:
            self._supervisors.for_component(request.component_id).execute(
                request.component_id, inverse, actor=actor.name, change_id=change.change_id
            )
        except (
            SupervisorError,
            RuntimeContractError,
            InvalidTransitionError,
            UnsupportedActionError,
        ) as exc:
            return self._move(
                change,
                ChangeStatus.ROLLBACK_FAILED,
                actor.name,
                "rollback_failed",
                rollback=f"{reason}; {inverse.value} failed: {exc}",
            )
        self._registry.set_desired(
            request.component_id, self._previous_desired.pop(change.change_id, None)
        )
        return self._move(
            change,
            ChangeStatus.ROLLED_BACK,
            actor.name,
            "rolled_back",
            rollback=f"{reason}; {inverse.value} applied",
        )

    def _expect(self, change_id: str, status: ChangeStatus) -> ChangeRecord:
        change = self.get(change_id)
        if change.status is not status:
            raise ChangeError(f"{change_id} is {change.status.value}, not {status.value}")
        return change

    def _new(
        self,
        request: ChangeRequest,
        status: ChangeStatus,
        classification: ChangeClass,
        reasons: tuple[str, ...],
        impact: ImpactReport | None,
        now: datetime,
        *,
        old_version: str | None,
        new_version: str | None,
    ) -> ChangeRecord:
        change = ChangeRecord(
            request=request,
            status=status,
            classification=classification,
            reasons=reasons,
            impact=impact,
            old_version=old_version,
            new_version=new_version,
            created_at=now,
            updated_at=now,
        )
        self._changes[request.change_id] = change
        self._audit_entry(
            change, f"submitted:{status.value}", request.actor.name, "; ".join(reasons)
        )
        return change

    def _move(
        self, change: ChangeRecord, status: ChangeStatus, actor: str, event: str, **changes: Any
    ) -> ChangeRecord:
        updated = replace(change, status=status, updated_at=self._clock(), **changes)
        self._changes[change.change_id] = updated
        detail = str(changes.get("result") or changes.get("rollback") or status.value)
        self._audit_entry(updated, event, actor, detail)
        return updated

    def _audit_entry(self, change: ChangeRecord, event: str, actor: str, detail: str) -> None:
        self._append_audit(change.change_id, change.request.component_id, event, actor, detail)

    def _append_audit(
        self, change_id: str, component_id: str, event: str, actor: str, detail: str
    ) -> None:
        entry = AuditEntry(
            change_id=change_id,
            component_id=component_id,
            event=event,
            actor=actor,
            detail=detail,
            at=self._clock(),
        )
        self._audit.append(entry)
        log.info(
            "runtime.change_audit",
            change_id=entry.change_id,
            component_id=entry.component_id,
            audit_event=event,
            actor=actor,
        )
        for sink in self._sinks:
            try:
                sink(entry)
            except Exception as exc:  # the in-memory log is the record; a sink is a mirror
                log.error("runtime.audit_sink_failed", change_id=entry.change_id, error=str(exc))
