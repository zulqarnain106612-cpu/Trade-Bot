"""
Desired-versus-actual reconciliation.

``diff`` compares every component's desired state with its actual state and
classifies each mismatch:

* VERSION     -- it runs another version than the one desired;
* STATE       -- it is in another lifecycle state than the one desired;
* FAILED      -- it failed (a FAILED component is a mismatch whatever is desired);
* UNREACHABLE -- no sequence of actions its capabilities allow gets it there.

The ``Reconciler`` never mutates anything itself: each pass submits, per
mismatch, the next action of the shortest path as a change request through
the ``ChangeManager`` under the system actor. The change manager's policy
decides what happens -- a LIVE_SAFE step (taking risk off) executes at once;
anything else waits for a human approver, and the open change stops the next
pass from submitting a duplicate. Unreachable mismatches and rejected steps
are raised through the alert callback rather than retried blindly.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from src.runtime.changes import (
    ROLE_REQUESTER,
    Actor,
    ActorKind,
    ChangeManager,
    ChangeRequest,
    ChangeStatus,
)
from src.runtime.contracts import (
    TRANSITIONS,
    ComponentRecord,
    ComponentVersion,
    DesiredState,
    InvalidTransitionError,
    LifecycleAction,
    LifecycleState,
    UnsupportedActionError,
    next_state,
)
from src.runtime.registry import RuntimeRegistry

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

A = LifecycleAction

# Actions that do not move a component between states and so never help a
# state path; QUARANTINE is only a path step when quarantine is the target.
_NOT_PATH_STEPS = frozenset({A.RELOAD, A.REPLACE, A.ROLLBACK, A.DISCOVER})

RECONCILER = Actor("reconciler", ActorKind.SYSTEM, frozenset({ROLE_REQUESTER}))

_FAILED_OUTCOMES = frozenset(
    {
        f"executed: {ChangeStatus.FAILED.value}",
        f"executed: {ChangeStatus.ROLLBACK_FAILED.value}",
        # Executed, found unhealthy and undone: the same step would only
        # repeat the round trip every pass.
        f"executed: {ChangeStatus.ROLLED_BACK.value}",
    }
)


def _signature(mismatch: Mismatch) -> tuple[object, ...]:
    """What makes a mismatch the same mismatch from one pass to the next."""
    desired = mismatch.desired
    return (
        mismatch.kind,
        mismatch.actual_state,
        mismatch.actual_version,
        desired.target_state,
        desired.target_version,
        desired.requested_at,
    )


class MismatchKind(StrEnum):
    VERSION = "VERSION"
    STATE = "STATE"
    FAILED = "FAILED"
    UNREACHABLE = "UNREACHABLE"


@dataclass(frozen=True, slots=True)
class Mismatch:
    component_id: str
    kind: MismatchKind
    desired: DesiredState
    actual_state: LifecycleState
    actual_version: str
    plan: tuple[LifecycleAction, ...]
    target_version: ComponentVersion | None = None

    @property
    def detail(self) -> str:
        want = self.desired.target_state.value
        if self.desired.target_version is not None:
            want += f"@{self.desired.target_version}"
        return (
            f"{self.component_id}: desired {want}, actual "
            f"{self.actual_state.value}@{self.actual_version} ({self.kind.value})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "kind": self.kind.value,
            "desired": self.desired.to_dict(),
            "actual_state": self.actual_state.value,
            "actual_version": self.actual_version,
            "plan": [a.value for a in self.plan],
            "detail": self.detail,
        }


def plan_path(
    record: ComponentRecord, target: LifecycleState
) -> tuple[LifecycleAction, ...] | None:
    """Shortest action sequence from the record's state to ``target`` (BFS)."""
    start = record.state
    if start is target:
        return ()
    caps = record.spec.capabilities
    queue: deque[tuple[LifecycleState, tuple[LifecycleAction, ...]]] = deque([(start, ())])
    seen = {start}
    while queue:
        state, path = queue.popleft()
        for (source, action), _ in sorted(TRANSITIONS.items(), key=lambda kv: kv[0][1].value):
            if source is not state or action in _NOT_PATH_STEPS:
                continue
            if action is A.QUARANTINE and target is not LifecycleState.QUARANTINED:
                continue
            try:
                after = next_state(record.component_id, state, action, caps)
            except (InvalidTransitionError, UnsupportedActionError):
                continue
            if after in seen:
                continue
            if after is target:
                return (*path, action)
            seen.add(after)
            queue.append((after, (*path, action)))
    return None


def diff(registry: RuntimeRegistry) -> list[Mismatch]:
    mismatches = []
    for record in registry.components():
        desired = record.desired
        if desired is None:
            continue
        if desired.target_version is not None and desired.target_version != record.version.version:
            versions = {v.version: v for v in registry.versions(record.component_id)}
            target = versions[desired.target_version]
            action = A.ROLLBACK if target in record.previous_versions else A.REPLACE
            mismatches.append(
                Mismatch(
                    record.component_id,
                    MismatchKind.VERSION,
                    desired,
                    record.state,
                    record.version.version,
                    (action,),
                    target,
                )
            )
            continue
        if record.state is desired.target_state:
            continue
        plan = plan_path(record, desired.target_state)
        if plan is None:
            kind = MismatchKind.UNREACHABLE
        elif record.state is LifecycleState.FAILED:
            kind = MismatchKind.FAILED
        else:
            kind = MismatchKind.STATE
        mismatches.append(
            Mismatch(
                record.component_id,
                kind,
                desired,
                record.state,
                record.version.version,
                plan or (),
            )
        )
    return mismatches


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    mismatch: Mismatch
    outcome: str
    change_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.mismatch.to_dict(), "outcome": self.outcome, "change_id": self.change_id}


class Reconciler:
    def __init__(
        self,
        registry: RuntimeRegistry,
        changes: ChangeManager,
        *,
        alert: Callable[[str], None] | None = None,
        actor: Actor = RECONCILER,
        retry_alerted_after: int = 60,
    ) -> None:
        if retry_alerted_after < 1:
            raise ValueError("retry_alerted_after must be >= 1")
        self._registry = registry
        self._changes = changes
        self._alert = alert
        self._actor = actor
        self._retry_alerted_after = retry_alerted_after
        self._passes = 0
        # Per component: the mismatch last alerted on and the pass it was
        # alerted in (see reconcile_once's suppress_repeats).
        self._alerted: dict[str, tuple[tuple[object, ...], int]] = {}

    def _raise_alert(self, message: str) -> None:
        log.warning("runtime.reconcile_alert", detail=message)
        if self._alert is not None:
            self._alert(message)

    def reconcile_once(self, *, suppress_repeats: bool = False) -> list[ReconcileOutcome]:
        """
        One pass: at most one step per mismatched component.

        ``suppress_repeats`` is for the continuous loop. A mismatch that was
        already alerted on -- unreachable, a rejected step, a step that failed
        or was refused -- is not submitted again while it stays exactly the
        same: the alert is out, and resubmitting every pass would only grow
        the change log with identical rejections. It is retried as soon as
        the component's state or version or the desired state changes, after
        ``retry_alerted_after`` passes (a refusal can be transient: a tick
        held a lock), and on every manual pass (the default).
        """
        self._passes += 1
        mismatches = diff(self._registry)
        live = {m.component_id for m in mismatches}
        for component_id in [c for c in self._alerted if c not in live]:
            del self._alerted[component_id]
        outcomes = []
        for mismatch in mismatches:
            signature = _signature(mismatch)
            alerted = self._alerted.get(mismatch.component_id)
            if (
                suppress_repeats
                and alerted is not None
                and alerted[0] == signature
                and self._passes - alerted[1] < self._retry_alerted_after
            ):
                outcomes.append(ReconcileOutcome(mismatch, "suppressed: alerted, unchanged"))
                continue
            outcome = self._step(mismatch)
            if outcome.outcome.startswith("alerted") or outcome.outcome in _FAILED_OUTCOMES:
                self._alerted[mismatch.component_id] = (signature, self._passes)
            else:
                self._alerted.pop(mismatch.component_id, None)
            outcomes.append(outcome)
        return outcomes

    def _step(self, mismatch: Mismatch) -> ReconcileOutcome:
        if mismatch.kind is MismatchKind.UNREACHABLE:
            self._raise_alert(f"unreachable: {mismatch.detail}")
            return ReconcileOutcome(mismatch, "alerted: unreachable")
        open_change = self._changes.open_change(mismatch.component_id)
        if open_change is not None:
            return ReconcileOutcome(
                mismatch, f"pending: {open_change.status.value}", open_change.change_id
            )
        action = mismatch.plan[0]
        request = ChangeRequest(
            component_id=mismatch.component_id,
            action=action,
            actor=self._actor,
            reason=f"reconcile: {mismatch.detail}",
            target_version=mismatch.target_version if action is A.REPLACE else None,
            expected_version=mismatch.actual_version,
        )
        change = self._changes.submit(request)
        if change.status is ChangeStatus.REJECTED:
            reasons = "; ".join(change.reasons)
            self._raise_alert(f"rejected {action.value}: {mismatch.detail}: {reasons}")
            return ReconcileOutcome(mismatch, "alerted: rejected", change.change_id)
        if change.status is ChangeStatus.AWAITING_APPROVAL:
            return ReconcileOutcome(mismatch, "awaiting approval", change.change_id)
        # Only LIVE_SAFE changes arrive here APPROVED; everything else waited above.
        executed = self._changes.execute(change.change_id, self._actor)
        if executed.status in (ChangeStatus.FAILED, ChangeStatus.ROLLBACK_FAILED):
            self._raise_alert(f"{action.value} failed: {mismatch.detail}: {executed.result}")
        return ReconcileOutcome(mismatch, f"executed: {executed.status.value}", change.change_id)
