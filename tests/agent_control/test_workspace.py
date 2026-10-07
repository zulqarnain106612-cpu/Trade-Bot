"""
GOV-060 / GOV-061 / GOV-063 -- task operations against a real repository.

What these tests would catch:

* a task created on a detached HEAD or from a baseline that is not an
  ancestor, so "commits since baseline" means nothing;
* a COMPLETE task staying COMPLETE after HEAD moves;
* stopping with a dirty tree recorded as a clean pause (hidden WIP);
* resuming a task from a worktree it does not belong to;
* a validation counted although the tree changed or was dirty while it ran;
* commit, push and PR facts recorded without being checked against Git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent_control import workspace as workspace_module
from src.agent_control.gitstate import GitError
from src.agent_control.model import (
    CompletionState,
    DeliverableState,
    ManifestError,
    StepState,
    TaskPhase,
    TaskScope,
    ValidationRequirement,
)
from src.agent_control.store import TaskStore
from src.agent_control.workspace import ValidationRun, Workspace, _run_command


def _finish(repo, task_id: str) -> None:
    """Drive a make_task() task through every predicate (push included)."""
    ws = repo.workspace()
    repo.commit("feature.txt", "feature\n", "add feature")
    ws.verify_commit(task_id)
    ws.set_deliverable(task_id, "D-0001", DeliverableState.PUSHED)
    ws.finish_step(task_id, "S-0001")
    repo.push()
    ws.run_validation(task_id, "smoke")
    ws.audit_diff(task_id)


class TestCreation:
    def test_a_task_records_its_baseline_and_becomes_active(self, repo, make_task) -> None:
        task = make_task()
        assert task.task_id.startswith("TB-RUN-")
        assert task.baseline_sha == task.current_sha == repo.head()
        assert (task.branch, task.worktree) == ("feature", str(repo.path))
        assert task.checkpoints[0].reason == "baseline"
        assert repo.workspace().active_task_id() == task.task_id

    def test_a_detached_head_is_refused(self, repo) -> None:
        repo.git.run("switch", "-q", "--detach")
        with pytest.raises(ManifestError, match="not detached"):
            repo.workspace().create_task("o", scope=TaskScope())

    @pytest.mark.parametrize("baseline", ["abc123", "f" * 40])
    def test_a_baseline_must_be_a_full_known_sha(self, repo, baseline: str) -> None:
        with pytest.raises(ManifestError, match="not a full commit SHA"):
            repo.workspace().create_task("o", scope=TaskScope(), baseline_sha=baseline)

    def test_a_baseline_must_be_an_ancestor(self, repo) -> None:
        repo.git.run("switch", "-q", "main")
        other = repo.commit("main-only.txt")
        repo.git.run("switch", "-q", "feature")
        with pytest.raises(ManifestError, match="ancestor of HEAD"):
            repo.workspace().create_task("o", scope=TaskScope(), baseline_sha=other)

    def test_opening_from_a_linked_worktree_finds_the_same_store(
        self, repo, make_task, tmp_path: Path
    ) -> None:
        task = make_task()
        other = tmp_path / "linked"
        repo.git.run("worktree", "add", "-q", "-b", "linked", str(other))
        linked = Workspace.open(other)
        assert linked.store.load(task.task_id).task_id == task.task_id
        assert linked.active_task_id() is None
        with pytest.raises(ManifestError, match="belongs to worktree"):
            linked.resume(task.task_id)


class TestLifecycle:
    def test_completion_goes_stale_when_head_moves(self, repo, make_task) -> None:
        task = make_task()
        _finish(repo, task.task_id)
        ws = repo.workspace()
        report = ws.complete(task.task_id)
        assert report["complete"], report["blocking_reasons"]
        assert ws.load(None).status is CompletionState.COMPLETE
        repo.commit("later.txt")
        ws.checkpoint(None, "after more work")
        stale = ws.load(None)
        assert stale.status is CompletionState.STALE
        assert stale.completion_proof is None

    def test_an_incomplete_task_is_not_marked_complete(self, repo, make_task) -> None:
        task = make_task()
        report = repo.workspace().complete(task.task_id, offline=True)
        assert not report["complete"]
        assert "remote head not verified: not checked (offline)" in report["blocking_reasons"]
        assert repo.workspace().load(None).status is CompletionState.ACTIVE

    def test_a_dirty_pause_is_recorded_as_wip_with_every_path(self, repo, make_task) -> None:
        make_task()
        repo.write("a.txt")
        repo.write("b.txt")
        task = repo.workspace().stop(
            None, CompletionState.PAUSED, reason="end of day", next_action="finish a and b"
        )
        assert task.status is CompletionState.PAUSED_WITH_UNCOMMITTED_WIP
        assert task.status_history[-1].dirty_paths == ["a.txt", "b.txt"]
        assert task.checkpoints[-1].reason.startswith("PAUSED_WITH_UNCOMMITTED_WIP")

    def test_a_clean_pause_and_resume(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        paused = ws.stop(None, CompletionState.PAUSED, reason="r", next_action="n")
        assert paused.status is CompletionState.PAUSED
        assert ws.resume(None).status is CompletionState.ACTIVE
        assert ws.resume(None).status is CompletionState.ACTIVE

    def test_phases_follow_the_table(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        ws.set_phase(None, TaskPhase.PLANNING)
        assert ws.load(None).phase is TaskPhase.PLANNING

    def test_without_an_active_task_an_id_is_required(self, repo) -> None:
        with pytest.raises(ManifestError, match="no active task"):
            repo.workspace().load(None)

    def test_checkpoints_are_trimmed_and_read_back(
        self, repo, make_task, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_task()
        monkeypatch.setattr(workspace_module, "MAX_CHECKPOINTS", 2)
        ws = repo.workspace()
        ws.checkpoint(None, "one")
        last = ws.checkpoint(None, "two")
        assert [c.reason for c in ws.load(None).checkpoints] == ["one", "two"]
        assert ws.verify_checkpoint(None) == last

    def test_a_task_without_checkpoints_cannot_verify_one(self, repo, make_task) -> None:
        task = make_task()
        store = repo.workspace().store
        task.checkpoints.clear()
        store.save(task)
        with pytest.raises(ManifestError, match="has no checkpoint"):
            repo.workspace().verify_checkpoint(task.task_id)


class TestChecklistAndNotes:
    def test_steps(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        step = ws.add_step(None, " review ")
        assert (step.step_id, step.text) == ("S-0002", "review")
        with pytest.raises(ManifestError, match="needs text"):
            ws.add_step(None, " ")
        with pytest.raises(ManifestError, match="no step 'S-0099'"):
            ws.finish_step(None, "S-0099")
        with pytest.raises(ManifestError, match="unknown evidence"):
            ws.finish_step(None, "S-0002", evidence_ids=["EV-9"])
        done = ws.finish_step(None, "S-0002")
        assert done.state is StepState.DONE and done.done_at is not None

    def test_deliverables(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        item = ws.add_deliverable(None, "docs", ["docs/x.md"])
        assert (item.deliverable_id, item.paths) == ("D-0002", ["docs/x.md"])
        moved = ws.set_deliverable(None, "D-0002", DeliverableState.IMPLEMENTED)
        assert moved.state is DeliverableState.IMPLEMENTED

    def test_notes_and_next_action(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        ws.add_note(None, "finding", "f")
        ws.add_note(None, "rejected", "r")
        ws.add_note(None, "decision", "d")
        with pytest.raises(ManifestError, match="needs a kind"):
            ws.add_note(None, "gossip", "g")
        ws.set_next_action(None, "do x")
        assert ws.load(None).next_action == "do x"
        ws.set_next_action(None, " ")
        task = ws.load(None)
        assert task.next_action is None
        assert (task.findings, task.rejected_approaches, task.decisions) == (["f"], ["r"], ["d"])


class TestEvidence:
    def test_a_validation_on_a_clean_tree_counts(self, repo, make_task) -> None:
        make_task()
        record = repo.workspace().run_validation(None, "smoke")
        assert record.ok and record.exit_code == 0 and record.command == ["true"]

    def test_a_dirty_or_moving_tree_does_not_count(self, repo, make_task) -> None:
        make_task()
        repo.write("dirty.txt")

        def passes(command, cwd):
            return ValidationRun(tuple(command), 0, "fine")

        record = repo.workspace().run_validation(None, "smoke", execute=passes)
        assert not record.ok
        assert record.summary.startswith("NOT COUNTED")

    def test_unknown_or_ci_only_validations_cannot_run(self, repo) -> None:
        repo.workspace().create_task(
            "o", scope=TaskScope(), validations=[ValidationRequirement("ci-gates")]
        )
        ws = repo.workspace()
        for name in ("ci-gates", "nope"):
            with pytest.raises(ManifestError, match="no runnable validation"):
                ws.run_validation(None, name)

    def test_commands_that_cannot_start_report_127(self, tmp_path: Path) -> None:
        run = _run_command([str(tmp_path / "missing-binary")], tmp_path)
        assert run.exit_code == 127 and "could not run" in run.output

    def test_ci_evidence_is_attested_against_the_sha_it_ran_on(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        sha = "1" * 40
        with pytest.raises(ManifestError, match="must be one of"):
            ws.record_ci(None, "smoke", state="green", head_sha=sha, reference="u")
        with pytest.raises(ManifestError, match="full head SHA"):
            ws.record_ci(None, "smoke", state="success", head_sha="abc", reference="u")
        with pytest.raises(ManifestError, match="no validation 'nope'"):
            ws.record_ci(None, "nope", state="success", head_sha=sha, reference="u")
        record = ws.record_ci(None, "smoke", state="failure", head_sha=sha, reference=" url ")
        assert (record.ok, record.head_sha, record.tree_clean) == (False, sha, True)
        task = ws.load(None)
        assert (task.delivery.ci_state, task.delivery.ci_reference) == ("failure", "url")


class TestGit:
    def test_guard_spends_an_authorization(self, repo, make_task) -> None:
        ws = repo.workspace()
        assert ws.guard("git checkout main") == []
        make_task()
        assert ws.guard("git status") == []
        assert ws.guard("git checkout main") == ["checkout"]
        grant, point = ws.authorize(None, "checkout", "inspect main")
        assert (grant, point) == ("AU-0001", "CP-0002")
        assert ws.load(None).checkpoints[-1].reason == "pre-mutation: checkout"
        assert ws.guard("git checkout main") == []
        assert ws.guard("git checkout main") == ["checkout"]

    def test_finished_tasks_guard_nothing(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        ws.stop(None, CompletionState.ABANDONED, reason="superseded")
        assert ws.guard("git checkout main") == []

    def test_commit_verification(self, repo, make_task) -> None:
        task = make_task()
        ws = repo.workspace()
        head = repo.commit("feature.txt")
        record = ws.verify_commit(None)
        assert (record.sha, record.changed_paths, record.branch_head_matched) == (
            head,
            ["feature.txt"],
            True,
        )
        older = ws.verify_commit(None, task.baseline_sha)
        assert not older.branch_head_matched
        again = ws.verify_commit(None)
        assert [c.sha for c in ws.load(None).commits] == [task.baseline_sha, again.sha]

    def test_a_commit_outside_the_task_history_is_refused(self, repo, make_task) -> None:
        make_task()
        repo.git.run("switch", "-q", "--orphan", "elsewhere")
        orphan = repo.commit("o.txt")
        repo.git.run("switch", "-q", "feature")
        with pytest.raises(ManifestError, match="does not descend"):
            repo.workspace().verify_commit(None, orphan)

    def test_the_diff_audit_reports_whitespace_and_dirt(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        repo.commit("clean.txt", "fine\n")
        assert ws.audit_diff(None).ok
        repo.commit("bad.txt", "trailing \n")
        repo.write("dirty.txt")
        record = ws.audit_diff(None)
        assert not record.ok
        assert "whitespace errors" in record.summary and "tree not clean" in record.summary
        assert "bad.txt" in record.summary

    def test_push_and_pr_are_checked_against_head(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        head = repo.commit("feature.txt")
        assert not ws.verify_push(None).ok
        repo.push()
        pushed = ws.verify_push(None)
        assert pushed.ok and ws.load(None).delivery.remote_head_sha == head
        with pytest.raises(ManifestError, match="number, URL and full head SHA"):
            ws.record_pr(None, number=0, url="u", head_sha=head)
        assert not ws.record_pr(None, number=5, url="u", head_sha="2" * 40).ok
        assert ws.record_pr(None, number=5, url=" u ", head_sha=head).ok

    def test_an_unreachable_remote_is_reported_not_raised(self, repo, make_task) -> None:
        make_task()
        ws = repo.workspace()
        with ws.editing(None) as (task, _):
            task.delivery.remote = "nowhere"
        report = ws.verify_completion(None)
        assert any("remote head not verified" in r for r in report["blocking_reasons"])

    def test_a_required_branch_audit_is_evaluated(self, repo) -> None:
        ws = repo.workspace()
        ws.create_task("o", scope=TaskScope(requires_push=False, requires_branch_audit=True))
        report = ws.verify_completion(None)
        assert any("branch audit not reconciled" in r for r in report["blocking_reasons"])


class TestSummary:
    def test_no_active_task_means_no_summary(self, repo) -> None:
        assert repo.workspace().session_summary() == ""

    def test_the_summary_carries_what_a_new_session_needs(self, repo, make_task) -> None:
        task = make_task()
        ws = repo.workspace()
        ws.add_note(None, "decision", "use the existing bus")
        repo.write("wip.txt")
        ws.stop(None, CompletionState.PAUSED, reason="context full", next_action="commit wip")
        summary = ws.session_summary()
        for expected in (
            f"ACTIVE TASK: {task.task_id}",
            "STATUS: PAUSED_WITH_UNCOMMITTED_WIP",
            "OPEN BLOCKERS: PAUSED_WITH_UNCOMMITTED_WIP: context full; write the feature",
            "DIRTY FILES: wip.txt",
            "NEXT REQUIRED ACTION: commit wip",
            "DECISIONS: 1 recorded; latest: use the existing bus",
            "LAST CHECKPOINT: CP-0002",
        ):
            assert expected in summary


def test_opening_outside_a_repository_is_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        Workspace.open(tmp_path)


def test_the_store_lives_under_the_override(repo, state_dir: Path) -> None:
    assert repo.workspace().store.root == state_dir
    assert isinstance(repo.workspace().store, TaskStore)
