"""
Operations on a task, bound to one repository -- the API the CLI and hooks share.

Every operation takes the store lock once, loads the manifest, refreshes
``current_sha`` from Git, applies the change through the model's transition
functions and saves; an exception anywhere in between saves nothing. Every
fact recorded is therefore stamped with the commit it was true at, whichever
entry point recorded it.

Long-running work -- a validation command -- is never run under the lock:
the hooks take the same lock, and a validation that held it for minutes would
stall every tool call in the session.

Registry: GOV-060, GOV-061, GOV-063 (config/quality_registry.json).
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.command_schema import redact
from src.agent_control import branch_audit, completion, git_safety
from src.agent_control.gitstate import (
    GitError,
    GitLike,
    GitRunner,
    collect_snapshot,
    commit_info,
    commit_paths,
    is_ancestor,
    range_paths,
    ref_sha,
    remote_head,
)
from src.agent_control.model import (
    SCHEMA_VERSION,
    SHA_RE,
    Checkpoint,
    CommitRecord,
    CompletionState,
    Deliverable,
    DeliverableState,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    GitSnapshot,
    ManifestError,
    Step,
    StepState,
    TaskPhase,
    TaskRun,
    TaskScope,
    ValidationRequirement,
    advance_phase,
    change_status,
    next_id,
    set_deliverable_state,
)
from src.agent_control.store import TaskStore, default_state_dir

MAX_CHECKPOINTS = 100
SUMMARY_CHARS = 2000
VALIDATION_TIMEOUT_S = 1800.0
CI_STATES = ("success", "failure", "pending")


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).replace(microsecond=0)


def _bounded(text: str, limit: int = SUMMARY_CHARS) -> str:
    """Redacted and bounded: the tail is where a failing command says why."""
    clean = redact(text)[0]
    return clean if len(clean) <= limit else "..." + clean[-(limit - 3) :]


@dataclass(frozen=True, slots=True)
class ValidationRun:
    command: tuple[str, ...]
    exit_code: int
    output: str


class Workspace:
    def __init__(self, runner: GitLike, store: TaskStore) -> None:
        self.runner = runner
        self.store = store

    @classmethod
    def open(cls, cwd: Path | str) -> Workspace:
        runner = GitRunner(cwd)
        common = Path(runner.run("rev-parse", "--git-common-dir").stdout.strip())
        if not common.is_absolute():
            common = Path(cwd) / common
        return cls(runner, TaskStore(default_state_dir(common.resolve())))

    # -- reading ----------------------------------------------------------

    def snapshot(self) -> GitSnapshot:
        return collect_snapshot(self.runner)

    def toplevel(self) -> str:
        return self.runner.run("rev-parse", "--show-toplevel").stdout.strip()

    def active_task_id(self) -> str | None:
        return self.store.active_task_id(self.toplevel())

    def resolve_id(self, task_id: str | None) -> str:
        resolved = task_id or self.active_task_id()
        if resolved is None:
            raise ManifestError("no task id given and no active task in this worktree")
        return resolved

    def load(self, task_id: str | None) -> TaskRun:
        return self.store.load(self.resolve_id(task_id))

    # -- editing ----------------------------------------------------------

    @contextlib.contextmanager
    def editing(
        self, task_id: str | None, *, now: datetime | None = None
    ) -> Iterator[tuple[TaskRun, GitSnapshot]]:
        moment = _now(now).isoformat()
        with self.store.locked():
            task = self.store.load(self.resolve_id(task_id))
            snapshot = self.snapshot()
            self._refresh(task, snapshot, moment)
            yield task, snapshot
            task.updated_at = moment
            self.store.save(task)

    @staticmethod
    def _refresh(task: TaskRun, snapshot: GitSnapshot, now: str) -> None:
        if snapshot.repository_root != task.worktree or snapshot.head_sha is None:
            return
        if snapshot.head_sha == task.current_sha:
            return
        task.current_sha = snapshot.head_sha
        if task.status is CompletionState.COMPLETE:
            change_status(
                task,
                CompletionState.STALE,
                reason=f"HEAD moved to {snapshot.head_sha[:12]} after completion",
                now=now,
            )

    # -- lifecycle --------------------------------------------------------

    def create_task(
        self,
        objective: str,
        *,
        scope: TaskScope,
        deliverables: Sequence[tuple[str, Sequence[str]]] = (),
        validations: Sequence[ValidationRequirement] = (),
        steps: Sequence[str] = (),
        baseline_sha: str | None = None,
        now: datetime | None = None,
    ) -> TaskRun:
        moment = _now(now)
        stamp = moment.isoformat()
        snapshot = self.snapshot()
        if snapshot.branch is None or snapshot.head_sha is None:
            raise ManifestError("a task needs a branch with at least one commit (not detached)")
        baseline = baseline_sha or snapshot.head_sha
        if ref_sha(self.runner, baseline) != baseline or not SHA_RE.match(baseline):
            raise ManifestError(f"baseline {baseline!r} is not a full commit SHA here")
        if not is_ancestor(self.runner, baseline, snapshot.head_sha):
            raise ManifestError("baseline must be an ancestor of HEAD")
        with self.store.locked():
            task = TaskRun(
                schema_version=SCHEMA_VERSION,
                task_id=self.store.next_task_id(moment.date()),
                objective=objective.strip(),
                created_at=stamp,
                updated_at=stamp,
                repository=snapshot.git_common_dir,
                worktree=snapshot.repository_root,
                branch=snapshot.branch,
                baseline_sha=baseline,
                current_sha=snapshot.head_sha,
                phase=TaskPhase.CREATED,
                status=CompletionState.ACTIVE,
                scope=scope,
            )
            for description, paths in deliverables:
                task.deliverables.append(
                    Deliverable(
                        deliverable_id=next_id("D", (d.deliverable_id for d in task.deliverables)),
                        description=description,
                        paths=list(paths),
                        history=[f"{DeliverableState.REQUESTED}@{stamp}"],
                    )
                )
            task.validations.extend(validations)
            for text in steps:
                self._add_step(task, text)
            self._checkpoint(task, snapshot, "baseline", stamp)
            self.store.save(task)
            self.store.set_active(snapshot.repository_root, task.task_id)
        return task

    @staticmethod
    def _add_step(task: TaskRun, text: str) -> Step:
        if not text.strip():
            raise ManifestError("a checklist step needs text")
        step = Step(step_id=next_id("S", (s.step_id for s in task.steps)), text=text.strip())
        task.steps.append(step)
        return step

    @staticmethod
    def _checkpoint(task: TaskRun, snapshot: GitSnapshot, reason: str, now: str) -> Checkpoint:
        point = Checkpoint(
            checkpoint_id=next_id("CP", (c.checkpoint_id for c in task.checkpoints)),
            created_at=now,
            reason=reason,
            phase=task.phase,
            status=task.status,
            git=snapshot,
            open_steps=[s.step_id for s in task.open_steps],
            next_action=task.next_action,
            findings=list(task.findings),
            rejected_approaches=list(task.rejected_approaches),
        )
        task.checkpoints.append(point)
        del task.checkpoints[:-MAX_CHECKPOINTS]
        return point

    def checkpoint(
        self, task_id: str | None, reason: str, *, now: datetime | None = None
    ) -> Checkpoint:
        with self.editing(task_id, now=now) as (task, snapshot):
            return self._checkpoint(task, snapshot, reason, task.updated_at)

    def verify_checkpoint(self, task_id: str | None) -> Checkpoint:
        """Re-read the latest checkpoint through a fresh store, as a new process would."""
        task = TaskStore(self.store.root).load(self.resolve_id(task_id))
        if not task.checkpoints:
            raise ManifestError(f"{task.task_id} has no checkpoint")
        return task.checkpoints[-1]

    def set_phase(
        self, task_id: str | None, phase: TaskPhase, *, now: datetime | None = None
    ) -> None:
        with self.editing(task_id, now=now) as (task, _):
            advance_phase(task, phase, now=task.updated_at)

    def stop(
        self,
        task_id: str | None,
        target: CompletionState,
        *,
        reason: str,
        next_action: str | None = None,
        now: datetime | None = None,
    ) -> TaskRun:
        """Pause, block, fail or abandon. A dirty tree turns PAUSED into the WIP state."""
        with self.editing(task_id, now=now) as (task, snapshot):
            tree = next((w for w in snapshot.worktrees if w.path == task.worktree), None)
            dirty = (tree.dirty_paths or []) if tree else []
            if target is CompletionState.PAUSED and dirty:
                target = CompletionState.PAUSED_WITH_UNCOMMITTED_WIP
            change_status(
                task,
                target,
                reason=reason,
                now=task.updated_at,
                next_action=next_action,
                dirty_paths=dirty if target is CompletionState.PAUSED_WITH_UNCOMMITTED_WIP else (),
            )
            self._checkpoint(task, snapshot, f"{target}: {reason}", task.updated_at)
        return task

    def resume(self, task_id: str | None, *, now: datetime | None = None) -> TaskRun:
        with self.editing(task_id, now=now) as (task, snapshot):
            if snapshot.repository_root != task.worktree:
                raise ManifestError(f"{task.task_id} belongs to worktree {task.worktree}")
            if task.status is not CompletionState.ACTIVE:
                change_status(task, CompletionState.ACTIVE, reason="resumed", now=task.updated_at)
            self.store.set_active(task.worktree, task.task_id)
        return task

    # -- checklist, deliverables, notes -----------------------------------

    def add_step(self, task_id: str | None, text: str, *, now: datetime | None = None) -> Step:
        with self.editing(task_id, now=now) as (task, _):
            return self._add_step(task, text)

    def finish_step(
        self,
        task_id: str | None,
        step_id: str,
        *,
        evidence_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> Step:
        with self.editing(task_id, now=now) as (task, _):
            step = next((s for s in task.steps if s.step_id == step_id), None)
            if step is None:
                raise ManifestError(f"{task.task_id} has no step {step_id!r}")
            known = {e.evidence_id for e in task.evidence}
            unknown = [ev for ev in evidence_ids if ev not in known]
            if unknown:
                raise ManifestError(f"unknown evidence id(s): {unknown}")
            step.state = StepState.DONE
            step.done_at = task.updated_at
            step.evidence_ids = list(evidence_ids)
            return step

    def add_deliverable(
        self,
        task_id: str | None,
        description: str,
        paths: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Deliverable:
        with self.editing(task_id, now=now) as (task, _):
            deliverable = Deliverable(
                deliverable_id=next_id("D", (d.deliverable_id for d in task.deliverables)),
                description=description,
                paths=list(paths),
                history=[f"{DeliverableState.REQUESTED}@{task.updated_at}"],
            )
            task.deliverables.append(deliverable)
            return deliverable

    def set_deliverable(
        self,
        task_id: str | None,
        deliverable_id: str,
        state: DeliverableState,
        *,
        allow_regress: bool = False,
        now: datetime | None = None,
    ) -> Deliverable:
        with self.editing(task_id, now=now) as (task, _):
            return set_deliverable_state(
                task, deliverable_id, state, now=task.updated_at, allow_regress=allow_regress
            )

    def add_note(
        self, task_id: str | None, kind: str, text: str, *, now: datetime | None = None
    ) -> None:
        fields = {"finding": "findings", "rejected": "rejected_approaches", "decision": "decisions"}
        if kind not in fields or not text.strip():
            raise ManifestError(f"a note needs a kind in {sorted(fields)} and text")
        with self.editing(task_id, now=now) as (task, _):
            getattr(task, fields[kind]).append(text.strip())

    def set_next_action(
        self, task_id: str | None, text: str, *, now: datetime | None = None
    ) -> None:
        with self.editing(task_id, now=now) as (task, _):
            task.next_action = text.strip() or None

    # -- evidence ---------------------------------------------------------

    @staticmethod
    def _evidence(
        task: TaskRun,
        snapshot: GitSnapshot,
        *,
        kind: EvidenceKind,
        source: EvidenceSource,
        subject: str,
        ok: bool,
        now: str,
        **extra: Any,
    ) -> Evidence:
        record = Evidence(
            evidence_id=next_id("EV", (e.evidence_id for e in task.evidence)),
            kind=kind,
            source=source,
            subject=subject,
            ok=ok,
            recorded_at=now,
            head_sha=snapshot.head_sha,
            tree_clean=snapshot.is_clean,
            **extra,
        )
        task.evidence.append(record)
        return record

    def run_validation(
        self,
        task_id: str | None,
        name: str,
        *,
        execute: Callable[[Sequence[str], Path], ValidationRun] | None = None,
        now: datetime | None = None,
    ) -> Evidence:
        """Run a required validation's command and record its exit code as evidence."""
        task = self.load(task_id)
        requirement = next((v for v in task.validations if v.name == name), None)
        if requirement is None or not requirement.command:
            raise ManifestError(f"{task.task_id} has no runnable validation {name!r}")
        before = self.snapshot()
        run = (execute or _run_command)(requirement.command, self.runner.cwd)
        with self.editing(task.task_id, now=now) as (fresh, after):
            # Only a clean tree that did not move while the command ran is the
            # tree the command validated.
            steady = before.head_sha == after.head_sha and before.is_clean and after.is_clean
            note = "" if steady else "NOT COUNTED: tree dirty or HEAD moved during the run\n"
            return self._evidence(
                fresh,
                after,
                kind=EvidenceKind.COMMAND,
                source=EvidenceSource.EXECUTED,
                subject=name,
                ok=run.exit_code == 0 and steady,
                now=fresh.updated_at,
                command=list(run.command),
                exit_code=run.exit_code,
                summary=_bounded(note + run.output),
            )

    def record_ci(
        self,
        task_id: str | None,
        name: str,
        *,
        state: str,
        head_sha: str,
        reference: str,
        now: datetime | None = None,
    ) -> Evidence:
        """Attest a CI result read from the PR's CI notice comment (the sanctioned channel)."""
        if state not in CI_STATES:
            raise ManifestError(f"CI state must be one of {CI_STATES}")
        if not SHA_RE.match(head_sha) or not reference.strip():
            raise ManifestError("CI evidence needs the full head SHA it ran on and its source")
        with self.editing(task_id, now=now) as (task, snapshot):
            if name not in {v.name for v in task.validations}:
                raise ManifestError(f"{task.task_id} has no validation {name!r}")
            record = self._evidence(
                task,
                snapshot,
                kind=EvidenceKind.CI_NOTICE,
                source=EvidenceSource.ATTESTED,
                subject=name,
                ok=state == "success",
                now=task.updated_at,
                summary=f"CI {state} at {head_sha}",
                reference=reference.strip(),
            )
            # CI ran on a clean checkout of head_sha, whatever this tree looks like.
            record.head_sha = head_sha
            record.tree_clean = True
            task.delivery.ci_state = state
            task.delivery.ci_reference = reference.strip()
            return record

    # -- git --------------------------------------------------------------

    def authorize(
        self,
        task_id: str | None,
        operation: str,
        reason: str,
        *,
        ttl_s: int = git_safety.DEFAULT_TTL_S,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        """Checkpoint the pre-mutation state, then grant one guarded operation."""
        moment = _now(now)
        with self.editing(task_id, now=moment) as (task, snapshot):
            point = self._checkpoint(task, snapshot, f"pre-mutation: {operation}", task.updated_at)
            grant = git_safety.authorize(
                task,
                operation,
                reason,
                checkpoint_id=point.checkpoint_id,
                now=moment,
                ttl_s=ttl_s,
            )
            return grant.authorization_id, point.checkpoint_id

    def guard(self, command: str, *, now: datetime | None = None) -> list[str]:
        """Guarded operations ``command`` would run without authorization (consumes grants)."""
        operations = git_safety.guarded_operations(command)
        if not operations:
            return []
        task_id = self.active_task_id()
        if task_id is None:
            return []
        moment = _now(now)
        with self.store.locked():
            task = self.store.load(task_id)
            if task.status in (CompletionState.COMPLETE, CompletionState.ABANDONED):
                return []
            missing = git_safety.consume(task, operations, command, now=moment)
            if not missing:
                self.store.save(task)
            return missing

    def verify_commit(
        self, task_id: str | None, rev: str = "HEAD", *, now: datetime | None = None
    ) -> CommitRecord:
        with self.editing(task_id, now=now) as (task, snapshot):
            info = commit_info(self.runner, rev)
            if not is_ancestor(self.runner, task.baseline_sha, info.sha):
                raise ManifestError(f"{info.sha[:12]} does not descend from the task baseline")
            branch_head = ref_sha(self.runner, f"refs/heads/{task.branch}")
            record = CommitRecord(
                sha=info.sha,
                parents=list(info.parents),
                subject=info.subject,
                branch=task.branch,
                changed_paths=commit_paths(self.runner, info),
                verified_at=task.updated_at,
                branch_head_matched=branch_head == info.sha,
                tree_clean_after=snapshot.is_clean,
            )
            task.commits = [c for c in task.commits if c.sha != info.sha] + [record]
            # A verified commit is progress: the Stop gate's loop counter restarts.
            task.stop_blocks = 0
            self._evidence(
                task,
                snapshot,
                kind=EvidenceKind.COMMIT,
                source=EvidenceSource.OBSERVED,
                subject=f"commit {info.sha[:12]}",
                ok=record.branch_head_matched,
                now=task.updated_at,
                summary=f"{info.subject} ({len(record.changed_paths)} path(s))",
                reference=info.sha,
            )
            return record

    def audit_diff(self, task_id: str | None, *, now: datetime | None = None) -> Evidence:
        """The final diff audit: whitespace check and changed-file inventory, baseline..HEAD."""
        with self.editing(task_id, now=now) as (task, snapshot):
            head = snapshot.head_sha or task.current_sha
            check = self.runner.run("diff", "--check", task.baseline_sha, head, check=False)
            changed = range_paths(self.runner, task.baseline_sha, head)
            problems = []
            if check.returncode != 0:
                problems.append("whitespace errors:\n" + check.stdout)
            if not snapshot.is_clean:
                problems.append(f"tree not clean ({len(snapshot.dirty_paths)} path(s))")
            lines = [*problems, f"{len(changed)} changed path(s):", *changed]
            return self._evidence(
                task,
                snapshot,
                kind=EvidenceKind.DIFF_AUDIT,
                source=EvidenceSource.OBSERVED,
                subject=f"{task.baseline_sha[:12]}..{head[:12]}",
                ok=not problems,
                now=task.updated_at,
                summary=_bounded("\n".join(lines)),
            )

    def verify_push(
        self, task_id: str | None, *, remote: str = "origin", now: datetime | None = None
    ) -> Evidence:
        with self.editing(task_id, now=now) as (task, snapshot):
            head = remote_head(self.runner, remote, task.branch)
            task.delivery.remote = remote
            task.delivery.remote_branch = task.branch
            task.delivery.remote_head_sha = head
            task.delivery.push_verified_at = task.updated_at
            return self._evidence(
                task,
                snapshot,
                kind=EvidenceKind.PUSH,
                source=EvidenceSource.OBSERVED,
                subject=f"{remote}/{task.branch}",
                ok=head == snapshot.head_sha,
                now=task.updated_at,
                summary=f"remote head {head or '(absent)'}; local HEAD {snapshot.head_sha}",
                reference=head,
            )

    def record_pr(
        self,
        task_id: str | None,
        *,
        number: int,
        url: str,
        head_sha: str,
        now: datetime | None = None,
    ) -> Evidence:
        if number < 1 or not url.strip() or not SHA_RE.match(head_sha):
            raise ManifestError("a PR record needs its number, URL and full head SHA")
        with self.editing(task_id, now=now) as (task, snapshot):
            task.delivery.pr_number = number
            task.delivery.pr_url = url.strip()
            task.delivery.pr_head_sha = head_sha
            return self._evidence(
                task,
                snapshot,
                kind=EvidenceKind.PULL_REQUEST,
                source=EvidenceSource.ATTESTED,
                subject=f"PR #{number}",
                ok=head_sha == snapshot.head_sha,
                now=task.updated_at,
                summary=f"PR #{number} head {head_sha}",
                reference=url.strip(),
            )

    # -- completion -------------------------------------------------------

    def branch_report(
        self, *, base: str, remote: str = "origin", include_remote_heads: bool = False
    ) -> dict[str, Any]:
        inventory = branch_audit.collect_inventory(
            self.runner, base=base, remote=remote, include_remote_heads=include_remote_heads
        )
        with self.store.locked():
            ledger = self.store.load_ledger()
            report = branch_audit.verify_ledger(ledger, inventory, now=_now(None).isoformat())
            self.store.save_ledger(ledger)
        return report

    def verify_completion(
        self,
        task_id: str | None,
        *,
        offline: bool = False,
        base: str = "origin/main",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        task = self.load(task_id)
        snapshot = self.snapshot()
        remote_sha: str | None = None
        remote_error: str | None = None
        if task.scope.requires_push:
            if offline:
                remote_error = "not checked (offline)"
            else:
                try:
                    remote_sha = remote_head(self.runner, task.delivery.remote, task.branch)
                except GitError as exc:
                    remote_error = str(exc)
        audit = self.branch_report(base=base) if task.scope.requires_branch_audit else None
        return completion.evaluate(
            task,
            snapshot,
            self.runner,
            now=_now(now).isoformat(),
            remote_head=remote_sha,
            remote_error=remote_error,
            branch_audit=audit,
        )

    def complete(
        self, task_id: str | None, *, offline: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        """Record COMPLETE if, and only if, the validator passes right now."""
        report = self.verify_completion(task_id, offline=offline, now=now)
        if report["complete"]:
            proof = {key: report[key] for key in ("complete", "head_sha", "validated_at")}
            with self.editing(report["task_id"], now=now) as (task, _):
                change_status(
                    task,
                    CompletionState.COMPLETE,
                    reason="completion validator passed",
                    now=task.updated_at,
                    completion_proof=proof,
                )
        return report

    def session_summary(self) -> str:
        """What a fresh session needs first: the active task and where it stopped."""
        task_id = self.active_task_id()
        if task_id is None:
            return ""
        task = self.store.load(task_id)
        snapshot = self.snapshot()
        checkpoint = "none"
        if task.checkpoints:
            last = task.checkpoints[-1]
            checkpoint = f"{last.checkpoint_id} at {last.created_at} ({last.reason})"
        decisions = f"{len(task.decisions)} recorded"
        if task.decisions:
            decisions += f"; latest: {task.decisions[-1]}"
        blockers = [s.text for s in task.open_steps]
        stopped = task.status not in (CompletionState.ACTIVE, CompletionState.COMPLETE)
        if stopped and task.status_history:
            blockers.insert(0, f"{task.status}: {task.status_history[-1].reason}")
        lines = [
            f"ACTIVE TASK: {task.task_id} -- {task.objective}",
            f"STATUS: {task.status}  PHASE: {task.phase}",
            f"BRANCH: {snapshot.branch or '(detached)'} (task branch {task.branch})",
            f"HEAD: {snapshot.head_sha} (baseline {task.baseline_sha[:12]})",
            f"OPEN BLOCKERS: {'; '.join(blockers) if blockers else 'none recorded'}",
            f"DIRTY FILES: {', '.join(snapshot.dirty_paths) if snapshot.dirty_paths else 'none'}",
            f"LAST CHECKPOINT: {checkpoint}",
            f"NEXT REQUIRED ACTION: {task.next_action or 'not recorded'}",
            f"DECISIONS: {decisions}",
            f"VERIFY: python3 scripts/agent_control.py verify-completion {task.task_id}",
        ]
        return "\n".join(lines)


def _run_command(command: Sequence[str], cwd: Path) -> ValidationRun:
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=VALIDATION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ValidationRun(tuple(command), 127, f"could not run: {exc}")
    return ValidationRun(tuple(command), proc.returncode, proc.stdout + proc.stderr)
