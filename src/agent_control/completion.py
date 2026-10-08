"""
The completion validator -- "done" as a conclusion drawn from evidence.

``evaluate`` checks every predicate a COMPLETE task must satisfy against Git
as it is *now* and the evidence recorded against the task, and returns each
failing predicate as an exact blocking reason. Nothing here trusts the task's
own phase or the session's narrative: a task in phase DELIVERED with a dirty
tree is incomplete, and the report says which path is dirty.

Evidence is bound to a commit. A validation that passed before the last
commit says nothing about the last commit, so only evidence recorded at the
current HEAD, with a clean tree, satisfies a required validation.

Registry: GOV-063 (config/quality_registry.json).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from src.agent_control.gitstate import GitError, GitLike, is_ancestor, range_paths
from src.agent_control.model import (
    CompletionState,
    DeliverableState,
    EvidenceKind,
    GitSnapshot,
    TaskRun,
)
from src.agent_control.reliability import gate as reliability_gate

_ORDER = tuple(DeliverableState)
_VALIDATION_KINDS = (EvidenceKind.COMMAND, EvidenceKind.CI_NOTICE)


@dataclass(frozen=True, slots=True)
class Predicate:
    name: str
    ok: bool
    detail: str


def _short(sha: str | None) -> str:
    return sha[:12] if sha else "(none)"


def _listing(items: Iterable[str], limit: int = 5) -> str:
    values = list(items)
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


class _Checks:
    def __init__(self) -> None:
        self.items: list[Predicate] = []

    def add(self, name: str, ok: bool, failure: str) -> None:
        self.items.append(Predicate(name, ok, "ok" if ok else failure))


def _reachable(runner: GitLike, sha: str, head: str) -> bool:
    try:
        return is_ancestor(runner, sha, head)
    except GitError:
        return False


def _commit_checks(checks: _Checks, task: TaskRun, runner: GitLike, head: str | None) -> None:
    checks.add(
        "commit-exists",
        head is not None and head != task.baseline_sha,
        f"no commit beyond baseline {_short(task.baseline_sha)}",
    )
    verified = {commit.sha for commit in task.commits}
    checks.add(
        "head-commit-verified",
        head in verified,
        f"HEAD {_short(head)} is not a verified commit (run: git verify-commit)",
    )
    lost = [c.sha for c in task.commits if head is None or not _reachable(runner, c.sha, head)]
    checks.add(
        "commits-in-history",
        not lost,
        f"recorded commit(s) no longer in HEAD's history: {_listing(map(_short, lost))}",
    )


def _deliverable_checks(checks: _Checks, task: TaskRun, runner: GitLike, head: str | None) -> None:
    target = DeliverableState.PUSHED if task.scope.requires_push else DeliverableState.COMMITTED
    lagging = [
        f"{d.deliverable_id} ({d.state})"
        for d in task.deliverables
        if _ORDER.index(d.state) < _ORDER.index(target)
    ]
    checks.add(
        "deliverables-state", not lagging, f"deliverable(s) below {target}: {_listing(lagging)}"
    )
    changed = set(range_paths(runner, task.baseline_sha, head)) if head else set()
    absent = [path for d in task.deliverables for path in d.paths if path not in changed]
    checks.add(
        "deliverable-paths-committed",
        not absent,
        f"deliverable path(s) unchanged between baseline and HEAD: {_listing(absent)}",
    )


def _evidence_checks(checks: _Checks, task: TaskRun, head: str | None) -> None:
    for requirement in task.validations:
        passing = [
            e
            for e in task.evidence
            if e.subject == requirement.name
            and e.kind in _VALIDATION_KINDS
            and e.ok
            and e.tree_clean
            and e.head_sha == head
        ]
        checks.add(
            f"validation:{requirement.name}",
            bool(passing),
            f"validation '{requirement.name}' has no passing evidence at HEAD {_short(head)}",
        )
    audited = [
        e
        for e in task.evidence
        if e.kind is EvidenceKind.DIFF_AUDIT and e.ok and e.head_sha == head
    ]
    checks.add(
        "final-diff-audit", bool(audited), f"final diff audit missing for HEAD {_short(head)}"
    )


def _delivery_checks(
    checks: _Checks,
    task: TaskRun,
    head: str | None,
    remote_head: str | None,
    remote_error: str | None,
) -> None:
    if task.scope.requires_push:
        where = f"{task.delivery.remote}/{task.branch}"
        failure = (
            f"remote head not verified: {remote_error}"
            if remote_error
            else f"{where} is at {_short(remote_head)}, HEAD is {_short(head)}"
        )
        checks.add("remote-head-verified", not remote_error and remote_head == head, failure)
    if task.scope.requires_pr:
        delivery = task.delivery
        pr_head = _short(delivery.pr_head_sha)
        failure = (
            "required PR not recorded"
            if delivery.pr_number is None
            else f"PR #{delivery.pr_number} head {pr_head} != HEAD {_short(head)}"
        )
        checks.add(
            "pull-request", delivery.pr_number is not None and delivery.pr_head_sha == head, failure
        )


def evaluate(
    task: TaskRun,
    snapshot: GitSnapshot,
    runner: GitLike,
    *,
    now: str,
    remote_head: str | None = None,
    remote_error: str | None = None,
    branch_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every completion predicate; ``complete`` is true only when none fails."""
    head = snapshot.head_sha
    checks = _Checks()
    checks.add(
        "task-active",
        task.status in (CompletionState.ACTIVE, CompletionState.COMPLETE),
        f"task status is {task.status}",
    )
    checks.add(
        "worktree",
        snapshot.repository_root == task.worktree,
        f"task belongs to {task.worktree}, this worktree is {snapshot.repository_root}",
    )
    checks.add(
        "branch",
        snapshot.branch == task.branch,
        f"on {snapshot.branch or '(detached HEAD)'}, the task branch is {task.branch}",
    )
    checks.add(
        "no-active-git-operation",
        not snapshot.active_operations,
        f"unresolved {', '.join(snapshot.active_operations)} in progress",
    )
    tracked = [e.path for e in snapshot.entries if e.kind in ("changed", "renamed", "unmerged")]
    checks.add("no-uncommitted-changes", not tracked, f"working tree dirty: {_listing(tracked)}")
    untracked = snapshot.untracked_paths
    checks.add("no-untracked-files", not untracked, f"untracked files: {_listing(untracked)}")
    open_steps = [f"{s.step_id} '{s.text}'" for s in task.open_steps]
    checks.add(
        "checklist-complete", not open_steps, f"open checklist item(s): {_listing(open_steps)}"
    )
    if task.scope.requires_commit:
        _commit_checks(checks, task, runner, head)
    _deliverable_checks(checks, task, runner, head)
    _evidence_checks(checks, task, head)
    _delivery_checks(checks, task, head, remote_head, remote_error)
    reliability = reliability_gate(
        task_id=task.task_id,
        head_sha=head,
        requires_ci=task.scope.requires_pr and task.delivery.pr_number is not None,
    )
    checks.add(
        "reliability",
        reliability.ok,
        "; ".join(reliability.blockers) or "reliability controls satisfied",
    )
    if task.scope.requires_branch_audit:
        report = branch_audit or {}
        keys = ("missing", "extra", "stale", "changed")
        problems = {key: report[key] for key in keys if report.get(key)}
        failure = f"branch audit not reconciled: {problems}" if report else "branch audit not run"
        checks.add("branch-audit", bool(report.get("reconciled")), failure)
    blocking = [p.detail for p in checks.items if not p.ok]
    return {
        "complete": not blocking,
        "task_id": task.task_id,
        "branch": snapshot.branch,
        "head_sha": head,
        "validated_at": now,
        "blocking_reasons": blocking,
        "predicates": [asdict(p) for p in checks.items],
    }
