"""
GOV-063 -- the completion validator.

Each test starts from a task that passes every predicate and breaks exactly
one thing, then asserts that the validator says *which* thing, in words an
agent can act on. What this would catch:

* a dirty tree, an untracked file, a merge in progress or an open checklist
  item that does not block completion;
* evidence from before the last commit (or from a dirty tree) satisfying a
  required validation;
* a recorded commit that history no longer contains (a rewritten branch);
* a push or PR that is not at HEAD being accepted as delivered;
* a required branch audit that never ran, or did not reconcile.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.agent_control.completion import evaluate
from src.agent_control.gitstate import GitResult
from src.agent_control.model import (
    SCHEMA_VERSION,
    CommitRecord,
    CompletionState,
    Deliverable,
    DeliverableState,
    Delivery,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    GitSnapshot,
    StatusEntry,
    Step,
    StepState,
    TaskPhase,
    TaskRun,
    TaskScope,
    ValidationRequirement,
)

BASE = "b" * 40
HEAD = "h" * 40
OLD = "0" * 40
NOW = "2026-10-07T00:00:00+00:00"


def _evidence(kind: EvidenceKind, subject: str, **overrides: object) -> Evidence:
    record = Evidence(
        evidence_id=f"EV-{subject}",
        kind=kind,
        source=EvidenceSource.EXECUTED,
        subject=subject,
        ok=True,
        recorded_at=NOW,
        head_sha=HEAD,
        tree_clean=True,
    )
    return replace(record, **overrides)


def _task(**overrides: object) -> TaskRun:
    task = TaskRun(
        schema_version=SCHEMA_VERSION,
        task_id="TB-RUN-20261007-0001",
        objective="o",
        created_at=NOW,
        updated_at=NOW,
        repository="/w/.git",
        worktree="/w",
        branch="feature",
        baseline_sha=BASE,
        current_sha=HEAD,
        phase=TaskPhase.DELIVERED,
        status=CompletionState.ACTIVE,
        scope=TaskScope(requires_pr=True, requires_branch_audit=True),
        deliverables=[
            Deliverable(
                deliverable_id="D-0001",
                description="d",
                paths=["f.txt"],
                state=DeliverableState.PUSHED,
            )
        ],
        validations=[ValidationRequirement("smoke", ["true"]), ValidationRequirement("ci")],
        steps=[Step(step_id="S-0001", text="s", state=StepState.DONE, done_at=NOW)],
        evidence=[
            _evidence(EvidenceKind.COMMAND, "smoke"),
            _evidence(EvidenceKind.CI_NOTICE, "ci", source=EvidenceSource.ATTESTED),
            _evidence(EvidenceKind.DIFF_AUDIT, "audit"),
        ],
        commits=[
            CommitRecord(
                sha=HEAD,
                parents=[BASE],
                subject="s",
                branch="feature",
                changed_paths=["f.txt"],
                verified_at=NOW,
                branch_head_matched=True,
                tree_clean_after=True,
            )
        ],
        delivery=Delivery(pr_number=4, pr_url="u", pr_head_sha=HEAD),
    )
    return replace(task, **overrides)


def _snapshot(**overrides: object) -> GitSnapshot:
    snapshot = GitSnapshot(
        repository_root="/w",
        git_common_dir="/w/.git",
        branch="feature",
        head_sha=HEAD,
        upstream="origin/feature",
        ahead=0,
        behind=0,
        entries=[],
        worktrees=[],
        active_operations=[],
        captured_at=NOW,
    )
    return replace(snapshot, **overrides)


@pytest.fixture
def git(fake_git, tmp_path: Path):
    return fake_git(
        {
            ("merge-base", "--is-ancestor", HEAD, HEAD): GitResult((), 0, "", ""),
            ("merge-base", "--is-ancestor", OLD, HEAD): GitResult((), 1, "", ""),
            ("merge-base", "--is-ancestor", "e" * 40, HEAD): GitResult((), 128, "", "bad"),
            ("merge-base", "--is-ancestor", HEAD, BASE): GitResult((), 1, "", ""),
            ("diff", "--name-only", "-z", BASE, HEAD): "f.txt\0",
            ("diff", "--name-only", "-z", BASE, BASE): "",
        },
        tmp_path,
    )


RECONCILED = {"reconciled": True, "missing": [], "extra": [], "stale": [], "changed": []}


def _blockers(git, task: TaskRun | None = None, snapshot: GitSnapshot | None = None, **kw):
    kw.setdefault("remote_head", HEAD)
    kw.setdefault("branch_audit", RECONCILED)
    report = evaluate(task or _task(), snapshot or _snapshot(), git, now=NOW, **kw)
    return report


def test_a_task_meeting_every_predicate_is_complete(git) -> None:
    report = _blockers(git)
    assert report["complete"], report["blocking_reasons"]
    assert report["blocking_reasons"] == []
    assert (report["task_id"], report["branch"], report["head_sha"]) == (
        "TB-RUN-20261007-0001",
        "feature",
        HEAD,
    )
    assert all(p["detail"] == "ok" for p in report["predicates"])


@pytest.mark.parametrize(
    ("task_change", "snapshot_change", "reason"),
    [
        ({"status": CompletionState.BLOCKED}, {}, "task status is BLOCKED"),
        ({}, {"repository_root": "/elsewhere"}, "task belongs to /w"),
        ({}, {"branch": None}, "on (detached HEAD), the task branch is feature"),
        ({}, {"active_operations": ["merge"]}, "unresolved merge in progress"),
        ({}, {"entries": [StatusEntry("changed", ".M", "f.txt")]}, "working tree dirty: f.txt"),
        ({}, {"entries": [StatusEntry("untracked", "??", "tmp.log")]}, "untracked files: tmp.log"),
        (
            {"steps": [Step(step_id="S-0001", text="write docs")]},
            {},
            "open checklist item(s): S-0001 'write docs'",
        ),
        ({}, {"head_sha": BASE}, "no commit beyond baseline"),
        ({"commits": []}, {}, "is not a verified commit"),
        (
            {"deliverables": [Deliverable(deliverable_id="D-0001", description="d")]},
            {},
            "deliverable(s) below PUSHED: D-0001 (REQUESTED)",
        ),
        (
            {
                "deliverables": [
                    Deliverable(
                        deliverable_id="D-0001",
                        description="d",
                        paths=["missing.txt"],
                        state=DeliverableState.PUSHED,
                    )
                ]
            },
            {},
            "unchanged between baseline and HEAD: missing.txt",
        ),
        ({"delivery": Delivery()}, {}, "required PR not recorded"),
        ({"delivery": Delivery(pr_number=4, pr_head_sha=OLD)}, {}, "PR #4 head 000000000000"),
    ],
)
def test_each_breach_blocks_with_its_reason(git, task_change, snapshot_change, reason) -> None:
    report = _blockers(git, _task(**task_change), _snapshot(**snapshot_change))
    assert not report["complete"]
    assert any(reason in blocker for blocker in report["blocking_reasons"]), report


def test_validation_evidence_must_be_at_head_clean_and_passing(git) -> None:
    for stale in (
        {"head_sha": OLD},
        {"tree_clean": False},
        {"ok": False},
        {"kind": EvidenceKind.COMMIT},
    ):
        evidence = [_evidence(EvidenceKind.COMMAND, "smoke", **stale)]
        evidence += [e for e in _task().evidence if e.subject != "smoke"]
        report = _blockers(git, _task(evidence=evidence))
        assert report["blocking_reasons"] == [
            "validation 'smoke' has no passing evidence at HEAD hhhhhhhhhhhh"
        ], stale


def test_the_final_diff_audit_must_be_for_head(git) -> None:
    evidence = [e for e in _task().evidence if e.kind is not EvidenceKind.DIFF_AUDIT]
    evidence.append(_evidence(EvidenceKind.DIFF_AUDIT, "audit", head_sha=OLD))
    report = _blockers(git, _task(evidence=evidence))
    assert report["blocking_reasons"] == ["final diff audit missing for HEAD hhhhhhhhhhhh"]


def test_rewritten_history_is_detected(git) -> None:
    lost = [
        replace(_task().commits[0], sha=OLD),
        replace(_task().commits[0], sha="e" * 40),
    ]
    report = _blockers(git, _task(commits=[*_task().commits, *lost]))
    assert report["blocking_reasons"] == [
        "recorded commit(s) no longer in HEAD's history: 000000000000, eeeeeeeeeeee"
    ]


def test_an_unborn_head_fails_the_commit_predicates_without_touching_git(git) -> None:
    report = _blockers(git, snapshot=_snapshot(head_sha=None))
    names = {p["name"] for p in report["predicates"] if not p["ok"]}
    assert {"commit-exists", "head-commit-verified", "commits-in-history"} <= names
    assert "deliverable-paths-committed" in names


def test_commit_predicates_are_skipped_when_no_commit_is_required(git) -> None:
    scope = TaskScope(requires_commit=False, requires_push=False)
    task = _task(scope=scope, commits=[], delivery=Delivery())
    report = _blockers(git, task, _snapshot(head_sha=BASE))
    names = {p["name"] for p in report["predicates"]}
    assert "commit-exists" not in names and "remote-head-verified" not in names
    assert "pull-request" not in names and "branch-audit" not in names


class TestDelivery:
    def test_the_remote_must_be_at_head(self, git) -> None:
        report = _blockers(git, remote_head=OLD)
        assert report["blocking_reasons"] == [
            "origin/feature is at 000000000000, HEAD is hhhhhhhhhhhh"
        ]

    def test_an_absent_remote_branch_is_named(self, git) -> None:
        report = _blockers(git, remote_head=None)
        assert report["blocking_reasons"] == ["origin/feature is at (none), HEAD is hhhhhhhhhhhh"]

    def test_an_unreachable_remote_is_not_assumed_fine(self, git) -> None:
        report = _blockers(git, remote_error="network down")
        assert report["blocking_reasons"] == ["remote head not verified: network down"]


class TestBranchAudit:
    def test_an_audit_that_never_ran_blocks(self, git) -> None:
        report = _blockers(git, branch_audit=None)
        assert report["blocking_reasons"] == ["branch audit not run"]

    def test_an_unreconciled_audit_names_the_gap(self, git) -> None:
        audit = {**RECONCILED, "reconciled": False, "stale": ["topic"]}
        report = _blockers(git, branch_audit=audit)
        assert report["blocking_reasons"] == ["branch audit not reconciled: {'stale': ['topic']}"]


def test_long_listings_are_truncated(git) -> None:
    entries = [StatusEntry("untracked", "??", f"f{i}") for i in range(7)]
    report = _blockers(git, snapshot=_snapshot(entries=entries))
    assert report["blocking_reasons"] == ["untracked files: f0, f1, f2, f3, f4 (+2 more)"]
