"""
Task, evidence, checkpoint and branch-audit records, and their transition laws.

Two axes, deliberately separate, because collapsing them is how a paused task
ends up reported as finished:

* ``TaskPhase`` -- where in the delivery pipeline the work is
  (DISCOVERING ... DELIVERED). Phases move along validated edges only.
* ``CompletionState`` -- whether the work is moving (ACTIVE) or has stopped,
  and why. COMPLETE is reachable only with a completion proof whose HEAD is
  the task's current HEAD; it is never a boolean someone sets.

Every record is a plain dataclass with a strict JSON codec: unknown keys,
wrong types and unknown enum values are refused rather than coerced, so a
hand-edited or truncated manifest fails loudly at load instead of producing a
task that looks valid and is not.

Registry: GOV-060 (config/quality_registry.json).
"""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

SCHEMA_VERSION = 1
TASK_ID_RE = re.compile(r"^TB-RUN-\d{8}-\d{4}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

T = TypeVar("T")


class ManifestError(ValueError):
    """A manifest or record violates the task-state contract."""


class TransitionError(ManifestError):
    """A phase, status or deliverable change the transition laws forbid."""


def utc_now() -> str:
    """ISO-8601 UTC, second precision -- the only timestamp format stored."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class TaskPhase(StrEnum):
    CREATED = "CREATED"
    DISCOVERING = "DISCOVERING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    AUDITING = "AUDITING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    COMMITTED = "COMMITTED"
    READY_TO_DELIVER = "READY_TO_DELIVER"
    DELIVERED = "DELIVERED"


class CompletionState(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"
    PAUSED = "PAUSED"
    PAUSED_WITH_UNCOMMITTED_WIP = "PAUSED_WITH_UNCOMMITTED_WIP"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    STALE = "STALE"
    ABANDONED = "ABANDONED"


class DeliverableState(StrEnum):
    REQUESTED = "REQUESTED"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    DELIVERED = "DELIVERED"


class DeliveryState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    PUSHED = "PUSHED"
    PR_OPEN = "PR_OPEN"


class EvidenceKind(StrEnum):
    COMMAND = "COMMAND"
    CI_NOTICE = "CI_NOTICE"
    GIT_SNAPSHOT = "GIT_SNAPSHOT"
    COMMIT = "COMMIT"
    DIFF_AUDIT = "DIFF_AUDIT"
    PUSH = "PUSH"
    PULL_REQUEST = "PULL_REQUEST"


class EvidenceSource(StrEnum):
    # The tool ran the command itself and recorded its exit code.
    EXECUTED = "EXECUTED"
    # Read directly from Git or the remote by the tool.
    OBSERVED = "OBSERVED"
    # Recorded by the agent from an external channel (the PR CI notice).
    ATTESTED = "ATTESTED"


class StepState(StrEnum):
    OPEN = "OPEN"
    DONE = "DONE"


class BranchState(StrEnum):
    AUDITED_CLEAN = "AUDITED_CLEAN"
    AUDITED_WITH_COMMITTED_WORK = "AUDITED_WITH_COMMITTED_WORK"
    AUDITED_WITH_UNCOMMITTED_WORK = "AUDITED_WITH_UNCOMMITTED_WORK"
    AUDITED_WITH_CONFLICT = "AUDITED_WITH_CONFLICT"
    AUDITED_WITH_OPEN_PR = "AUDITED_WITH_OPEN_PR"
    AUDITED_WITH_CI_FAILURE = "AUDITED_WITH_CI_FAILURE"
    STALE = "STALE"
    DELETED = "DELETED"
    BLOCKED = "BLOCKED"


PHASE_TRANSITIONS: Mapping[TaskPhase, frozenset[TaskPhase]] = types.MappingProxyType(
    {
        TaskPhase.CREATED: frozenset({TaskPhase.DISCOVERING, TaskPhase.PLANNING}),
        TaskPhase.DISCOVERING: frozenset({TaskPhase.PLANNING}),
        TaskPhase.PLANNING: frozenset({TaskPhase.DISCOVERING, TaskPhase.IMPLEMENTING}),
        TaskPhase.IMPLEMENTING: frozenset({TaskPhase.PLANNING, TaskPhase.VERIFYING}),
        TaskPhase.VERIFYING: frozenset({TaskPhase.IMPLEMENTING, TaskPhase.AUDITING}),
        TaskPhase.AUDITING: frozenset({TaskPhase.IMPLEMENTING, TaskPhase.READY_TO_COMMIT}),
        TaskPhase.READY_TO_COMMIT: frozenset({TaskPhase.IMPLEMENTING, TaskPhase.COMMITTED}),
        TaskPhase.COMMITTED: frozenset({TaskPhase.IMPLEMENTING, TaskPhase.READY_TO_DELIVER}),
        TaskPhase.READY_TO_DELIVER: frozenset({TaskPhase.IMPLEMENTING, TaskPhase.DELIVERED}),
        TaskPhase.DELIVERED: frozenset({TaskPhase.IMPLEMENTING}),
    }
)

_RESUMABLE = frozenset({CompletionState.ACTIVE, CompletionState.ABANDONED})

STATUS_TRANSITIONS: Mapping[CompletionState, frozenset[CompletionState]] = types.MappingProxyType(
    {
        CompletionState.ACTIVE: frozenset(set(CompletionState) - {CompletionState.ACTIVE}),
        CompletionState.PAUSED: _RESUMABLE,
        CompletionState.PAUSED_WITH_UNCOMMITTED_WIP: _RESUMABLE,
        CompletionState.BLOCKED: _RESUMABLE,
        CompletionState.FAILED: _RESUMABLE,
        CompletionState.STALE: _RESUMABLE,
        CompletionState.COMPLETE: frozenset({CompletionState.STALE}),
        CompletionState.ABANDONED: frozenset(),
    }
)

# A stop that someone else has to pick up must say what to do next.
_NEEDS_NEXT_ACTION = frozenset(
    {
        CompletionState.PAUSED,
        CompletionState.PAUSED_WITH_UNCOMMITTED_WIP,
        CompletionState.BLOCKED,
    }
)

_DELIVERABLE_ORDER = tuple(DeliverableState)


@dataclass(slots=True)
class Step:
    step_id: str
    text: str
    state: StepState = StepState.OPEN
    done_at: str | None = None
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Deliverable:
    deliverable_id: str
    description: str
    paths: list[str] = field(default_factory=list)
    state: DeliverableState = DeliverableState.REQUESTED
    history: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationRequirement:
    name: str
    # argv run by ``evidence run``; None means only CI evidence can satisfy it.
    command: list[str] | None = None


@dataclass(slots=True)
class TaskScope:
    in_scope_paths: list[str] = field(default_factory=list)
    requires_commit: bool = True
    requires_push: bool = True
    requires_pr: bool = False
    requires_branch_audit: bool = False


@dataclass(slots=True)
class StatusEntry:
    kind: str
    xy: str
    path: str
    orig_path: str | None = None


@dataclass(slots=True)
class WorktreeState:
    path: str
    head_sha: str | None
    branch: str | None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    prunable: bool = False
    # None means the worktree was not inspected (bare, prunable, unreadable).
    dirty_paths: list[str] | None = None


@dataclass(slots=True)
class GitSnapshot:
    repository_root: str
    git_common_dir: str
    branch: str | None
    head_sha: str | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    entries: list[StatusEntry]
    worktrees: list[WorktreeState]
    active_operations: list[str]
    captured_at: str

    @property
    def dirty_paths(self) -> list[str]:
        return [e.path for e in self.entries if e.kind != "ignored"]

    @property
    def untracked_paths(self) -> list[str]:
        return [e.path for e in self.entries if e.kind == "untracked"]

    @property
    def staged_paths(self) -> list[str]:
        return [
            e.path
            for e in self.entries
            if e.kind in ("changed", "renamed") and e.xy[0] not in (".", "?")
        ]

    @property
    def unmerged_paths(self) -> list[str]:
        return [e.path for e in self.entries if e.kind == "unmerged"]

    @property
    def is_clean(self) -> bool:
        return not self.dirty_paths


@dataclass(slots=True)
class Evidence:
    evidence_id: str
    kind: EvidenceKind
    source: EvidenceSource
    subject: str
    ok: bool
    recorded_at: str
    head_sha: str | None
    tree_clean: bool
    command: list[str] | None = None
    exit_code: int | None = None
    summary: str = ""
    reference: str | None = None


@dataclass(slots=True)
class Checkpoint:
    checkpoint_id: str
    created_at: str
    reason: str
    phase: TaskPhase
    status: CompletionState
    git: GitSnapshot
    open_steps: list[str]
    next_action: str | None
    findings: list[str]
    rejected_approaches: list[str]


@dataclass(slots=True)
class CommitRecord:
    sha: str
    parents: list[str]
    subject: str
    branch: str | None
    changed_paths: list[str]
    verified_at: str
    branch_head_matched: bool
    tree_clean_after: bool


@dataclass(slots=True)
class Authorization:
    authorization_id: str
    operation: str
    reason: str
    created_at: str
    expires_at: str
    checkpoint_id: str
    consumed_at: str | None = None
    consumed_by: str | None = None


@dataclass(slots=True)
class Delivery:
    remote: str = "origin"
    remote_branch: str | None = None
    remote_head_sha: str | None = None
    push_verified_at: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_head_sha: str | None = None
    ci_state: str | None = None
    ci_reference: str | None = None


@dataclass(slots=True)
class StatusChange:
    at: str
    status: CompletionState
    reason: str
    next_action: str | None = None
    dirty_paths: list[str] = field(default_factory=list)
    head_sha: str | None = None


@dataclass(slots=True)
class TaskRun:
    schema_version: int
    task_id: str
    objective: str
    created_at: str
    updated_at: str
    repository: str
    worktree: str
    branch: str
    baseline_sha: str
    current_sha: str
    phase: TaskPhase
    status: CompletionState
    scope: TaskScope
    deliverables: list[Deliverable] = field(default_factory=list)
    validations: list[ValidationRequirement] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    commits: list[CommitRecord] = field(default_factory=list)
    authorizations: list[Authorization] = field(default_factory=list)
    delivery: Delivery = field(default_factory=Delivery)
    findings: list[str] = field(default_factory=list)
    rejected_approaches: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    status_history: list[StatusChange] = field(default_factory=list)
    next_action: str | None = None
    stop_blocks: int = 0
    completion_proof: dict[str, Any] | None = None

    @property
    def open_steps(self) -> list[Step]:
        return [s for s in self.steps if s.state is StepState.OPEN]

    @property
    def completed_steps(self) -> list[Step]:
        return [s for s in self.steps if s.state is StepState.DONE]


@dataclass(slots=True)
class BranchAuditEntry:
    branch_name: str
    remote_ref: str | None
    audit_id: str
    audited_head_sha: str | None
    audit_started_at: str
    audit_completed_at: str | None
    commit_history_status: str
    working_tree_status: str
    untracked_status: str
    index_status: str
    merge_state: str
    rebase_state: str
    related_pr: int | None
    related_issue: int | None
    finding: str
    action_taken: str
    verification_status: str
    completion_status: BranchState
    evidence: list[str] = field(default_factory=list)
    stale_since_sha: str | None = None
    pr_state: str | None = None
    ci_state: str | None = None


@dataclass(slots=True)
class BranchLedger:
    schema_version: int
    entries: list[BranchAuditEntry] = field(default_factory=list)

    def get(self, branch_name: str) -> BranchAuditEntry | None:
        for entry in self.entries:
            if entry.branch_name == branch_name:
                return entry
        return None


# --------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------


def to_dict(record: Any) -> dict[str, Any]:
    """A JSON-ready dict. StrEnum members serialise as their string value."""
    return dataclasses.asdict(record)


def from_dict(cls: type[T], data: object, where: str = "") -> T:
    """Decode ``data`` into ``cls``, refusing anything the dataclass does not declare."""
    label = where or cls.__name__
    if not isinstance(data, Mapping):
        raise ManifestError(f"{label}: expected an object, got {type(data).__name__}")
    declared = dataclasses.fields(cls)  # type: ignore[arg-type]
    unknown = sorted(set(data) - {f.name for f in declared})
    if unknown:
        raise ManifestError(f"{label}: unknown field(s) {unknown}")
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for spec in declared:
        if spec.name in data:
            kwargs[spec.name] = _decode(hints[spec.name], data[spec.name], f"{label}.{spec.name}")
        elif spec.default is dataclasses.MISSING and spec.default_factory is dataclasses.MISSING:
            raise ManifestError(f"{label}: missing required field {spec.name!r}")
    return cls(**kwargs)


def _decode(tp: Any, value: object, where: str) -> Any:
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        if value is None:
            return None
        (inner,) = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        return _decode(inner, value, where)
    if origin is list:
        if not isinstance(value, list):
            raise ManifestError(f"{where}: expected a list")
        (item,) = typing.get_args(tp)
        return [_decode(item, v, f"{where}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ManifestError(f"{where}: expected an object")
        return dict(value)
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value, where)
    if isinstance(tp, type) and issubclass(tp, StrEnum):
        try:
            return tp(value)
        except ValueError:
            raise ManifestError(f"{where}: {value!r} is not a valid {tp.__name__}") from None
    if tp is bool or tp is int or tp is str:
        if type(value) is not tp:
            raise ManifestError(f"{where}: expected {tp.__name__}, got {type(value).__name__}")
        return value
    raise ManifestError(f"{where}: unsupported field type {tp!r}")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _duplicates(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for item in ids:
        if item in seen:
            dupes.append(item)
        seen.add(item)
    return dupes


def task_problems(task: TaskRun) -> list[str]:
    """Every way ``task`` breaks the contract the codec cannot see. Empty = valid."""
    problems: list[str] = []
    if task.schema_version != SCHEMA_VERSION:
        problems.append(f"schema_version {task.schema_version} != {SCHEMA_VERSION}")
    if not TASK_ID_RE.match(task.task_id):
        problems.append(f"task_id {task.task_id!r} does not match TB-RUN-YYYYMMDD-NNNN")
    if not task.objective.strip():
        problems.append("objective is empty")
    for name in ("baseline_sha", "current_sha"):
        if not SHA_RE.match(getattr(task, name)):
            problems.append(f"{name} is not a 40-character lowercase SHA")
    collections = {
        "step": [s.step_id for s in task.steps],
        "deliverable": [d.deliverable_id for d in task.deliverables],
        "validation": [v.name for v in task.validations],
        "evidence": [e.evidence_id for e in task.evidence],
        "checkpoint": [c.checkpoint_id for c in task.checkpoints],
        "authorization": [a.authorization_id for a in task.authorizations],
    }
    for label, ids in collections.items():
        for dupe in _duplicates(ids):
            problems.append(f"duplicate {label} id {dupe!r}")
    known_evidence = set(collections["evidence"])
    for step in task.steps:
        for ev in step.evidence_ids:
            if ev not in known_evidence:
                problems.append(f"step {step.step_id} cites unknown evidence {ev!r}")
        if (step.state is StepState.DONE) != (step.done_at is not None):
            problems.append(f"step {step.step_id} done_at disagrees with its state")
    if task.status is CompletionState.COMPLETE:
        proof = task.completion_proof or {}
        if proof.get("complete") is not True or proof.get("head_sha") != task.current_sha:
            problems.append("status COMPLETE without a completion proof for current_sha")
    elif task.completion_proof is not None:
        problems.append(f"completion_proof present while status is {task.status}")
    if task.status is CompletionState.PAUSED_WITH_UNCOMMITTED_WIP:
        last = task.status_history[-1] if task.status_history else None
        if last is None or not last.dirty_paths:
            problems.append("PAUSED_WITH_UNCOMMITTED_WIP without the dirty paths it paused on")
    if task.stop_blocks < 0:
        problems.append("stop_blocks is negative")
    return problems


def load_task(data: object) -> TaskRun:
    task = from_dict(TaskRun, data)
    problems = task_problems(task)
    if problems:
        raise ManifestError(f"{task.task_id}: " + "; ".join(problems))
    return task


def load_ledger(data: object) -> BranchLedger:
    ledger = from_dict(BranchLedger, data)
    dupes = _duplicates(e.branch_name for e in ledger.entries)
    if dupes:
        raise ManifestError(f"branch ledger: duplicate entries {dupes}")
    return ledger


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def next_id(prefix: str, existing: Iterable[str]) -> str:
    """``PREFIX-NNNN`` one past the highest existing number for ``prefix``."""
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    numbers = [int(m.group(1)) for m in map(pattern.match, existing) if m]
    return f"{prefix}-{max(numbers, default=0) + 1:04d}"


def advance_phase(task: TaskRun, target: TaskPhase, *, now: str) -> None:
    if task.status is not CompletionState.ACTIVE:
        raise TransitionError(f"{task.task_id} is {task.status}; resume it before changing phase")
    if target not in PHASE_TRANSITIONS[task.phase]:
        allowed = ", ".join(sorted(PHASE_TRANSITIONS[task.phase]))
        raise TransitionError(f"phase {task.phase} -> {target} is not allowed (allowed: {allowed})")
    task.phase = target
    task.updated_at = now


def change_status(
    task: TaskRun,
    target: CompletionState,
    *,
    reason: str,
    now: str,
    next_action: str | None = None,
    dirty_paths: Iterable[str] = (),
    completion_proof: Mapping[str, Any] | None = None,
) -> None:
    dirty = sorted(dirty_paths)
    if target not in STATUS_TRANSITIONS[task.status]:
        raise TransitionError(f"status {task.status} -> {target} is not allowed")
    if not reason.strip():
        raise ManifestError("a status change needs a reason")
    if target in _NEEDS_NEXT_ACTION and not (next_action and next_action.strip()):
        raise ManifestError(f"{target} needs the next required action")
    if target is CompletionState.PAUSED and dirty:
        raise TransitionError("the work tree is dirty: pause as PAUSED_WITH_UNCOMMITTED_WIP")
    if target is CompletionState.PAUSED_WITH_UNCOMMITTED_WIP and not dirty:
        raise TransitionError("PAUSED_WITH_UNCOMMITTED_WIP needs the dirty paths")
    if target is CompletionState.COMPLETE:
        proof = dict(completion_proof or {})
        if proof.get("complete") is not True or proof.get("head_sha") != task.current_sha:
            raise TransitionError("COMPLETE needs a passing completion proof for current_sha")
        task.completion_proof = proof
    else:
        task.completion_proof = None
    task.status = target
    task.next_action = next_action if target is not CompletionState.ACTIVE else task.next_action
    task.stop_blocks = 0
    task.updated_at = now
    task.status_history.append(
        StatusChange(
            at=now,
            status=target,
            reason=reason,
            next_action=next_action,
            dirty_paths=dirty,
            head_sha=task.current_sha,
        )
    )


def set_deliverable_state(
    task: TaskRun,
    deliverable_id: str,
    state: DeliverableState,
    *,
    now: str,
    allow_regress: bool = False,
) -> Deliverable:
    for deliverable in task.deliverables:
        if deliverable.deliverable_id == deliverable_id:
            break
    else:
        raise ManifestError(f"{task.task_id} has no deliverable {deliverable_id!r}")
    old = _DELIVERABLE_ORDER.index(deliverable.state)
    new = _DELIVERABLE_ORDER.index(state)
    if new < old and not allow_regress:
        raise TransitionError(
            f"deliverable {deliverable_id} {deliverable.state} -> {state} moves backwards; "
            "pass allow_regress to record rework"
        )
    deliverable.state = state
    deliverable.history.append(f"{state}@{now}")
    task.updated_at = now
    return deliverable


def delivery_state(task: TaskRun) -> DeliveryState:
    if not task.scope.requires_push:
        return DeliveryState.NOT_REQUIRED
    if task.delivery.remote_head_sha != task.current_sha:
        return DeliveryState.PENDING
    if task.delivery.pr_number is not None and task.delivery.pr_head_sha == task.current_sha:
        return DeliveryState.PR_OPEN
    return DeliveryState.PUSHED
