"""
GOV-060 -- the agent task record and its transition laws.

What these tests would catch:

* a status change that skips its law: pausing a dirty tree as plain PAUSED
  (hiding the WIP), stopping without saying what to do next, or reaching
  COMPLETE without a proof bound to the current HEAD;
* a phase jump (CREATED -> DELIVERED) that makes a task look further along
  than it is;
* a manifest that decodes when it should not -- an unknown key, a bool where
  an int belongs, an enum value that does not exist -- and so loads as a task
  that looks valid and is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from src.agent_control.model import (
    PHASE_TRANSITIONS,
    SCHEMA_VERSION,
    STATUS_TRANSITIONS,
    BranchAuditEntry,
    BranchLedger,
    BranchState,
    Checkpoint,
    CompletionState,
    Deliverable,
    DeliverableState,
    DeliveryState,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    GitSnapshot,
    ManifestError,
    StatusEntry,
    Step,
    StepState,
    TaskPhase,
    TaskRun,
    TaskScope,
    TransitionError,
    advance_phase,
    change_status,
    delivery_state,
    from_dict,
    load_ledger,
    load_task,
    next_id,
    set_deliverable_state,
    task_problems,
    to_dict,
    utc_now,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = "2026-10-07T00:00:00+00:00"


def _snapshot(entries: list[StatusEntry] | None = None) -> GitSnapshot:
    return GitSnapshot(
        repository_root="/work",
        git_common_dir="/work/.git",
        branch="feature",
        head_sha=SHA_A,
        upstream=None,
        ahead=None,
        behind=None,
        entries=entries or [],
        worktrees=[],
        active_operations=[],
        captured_at=NOW,
    )


def _task(**overrides: object) -> TaskRun:
    task = TaskRun(
        schema_version=SCHEMA_VERSION,
        task_id="TB-RUN-20261007-0001",
        objective="ship it",
        created_at=NOW,
        updated_at=NOW,
        repository="/work/.git",
        worktree="/work",
        branch="feature",
        baseline_sha=SHA_B,
        current_sha=SHA_A,
        phase=TaskPhase.CREATED,
        status=CompletionState.ACTIVE,
        scope=TaskScope(),
    )
    return replace(task, **overrides)


def _proof(head: str = SHA_A) -> dict[str, object]:
    return {"complete": True, "head_sha": head, "validated_at": NOW}


class TestPhases:
    def test_every_phase_has_a_transition_row(self) -> None:
        assert set(PHASE_TRANSITIONS) == set(TaskPhase)

    def test_a_valid_edge_advances(self) -> None:
        task = _task()
        advance_phase(task, TaskPhase.PLANNING, now=NOW)
        assert task.phase is TaskPhase.PLANNING

    def test_a_jump_is_refused(self) -> None:
        with pytest.raises(TransitionError, match="CREATED -> DELIVERED"):
            advance_phase(_task(), TaskPhase.DELIVERED, now=NOW)

    def test_a_stopped_task_cannot_change_phase(self) -> None:
        with pytest.raises(TransitionError, match="resume it"):
            advance_phase(_task(status=CompletionState.BLOCKED), TaskPhase.PLANNING, now=NOW)


class TestStatus:
    def test_every_status_has_a_transition_row(self) -> None:
        assert set(STATUS_TRANSITIONS) == set(CompletionState)
        assert STATUS_TRANSITIONS[CompletionState.ABANDONED] == frozenset()

    def test_pause_records_reason_next_action_and_history(self) -> None:
        task = _task(stop_blocks=3)
        change_status(task, CompletionState.PAUSED, reason="lunch", now=NOW, next_action="resume")
        assert task.status is CompletionState.PAUSED
        assert task.next_action == "resume"
        assert task.stop_blocks == 0
        assert task.status_history[-1].reason == "lunch"
        assert task.status_history[-1].head_sha == SHA_A

    def test_resume_keeps_the_recorded_next_action(self) -> None:
        task = _task()
        change_status(task, CompletionState.BLOCKED, reason="r", now=NOW, next_action="ask owner")
        change_status(task, CompletionState.ACTIVE, reason="resumed", now=NOW)
        assert task.next_action == "ask owner"

    def test_a_forbidden_edge_is_refused(self) -> None:
        with pytest.raises(TransitionError, match="ABANDONED -> ACTIVE"):
            change_status(
                _task(status=CompletionState.ABANDONED),
                CompletionState.ACTIVE,
                reason="r",
                now=NOW,
            )

    def test_a_status_change_needs_a_reason(self) -> None:
        with pytest.raises(ManifestError, match="needs a reason"):
            change_status(_task(), CompletionState.FAILED, reason="  ", now=NOW)

    @pytest.mark.parametrize(
        "target",
        [
            CompletionState.PAUSED,
            CompletionState.BLOCKED,
            CompletionState.PAUSED_WITH_UNCOMMITTED_WIP,
        ],
    )
    def test_a_handover_stop_needs_the_next_action(self, target: CompletionState) -> None:
        with pytest.raises(ManifestError, match="next required action"):
            change_status(_task(), target, reason="r", now=NOW, dirty_paths=["a"])

    def test_a_dirty_tree_cannot_be_paused_as_clean(self) -> None:
        with pytest.raises(TransitionError, match="PAUSED_WITH_UNCOMMITTED_WIP"):
            change_status(
                _task(),
                CompletionState.PAUSED,
                reason="r",
                now=NOW,
                next_action="n",
                dirty_paths=["a"],
            )

    def test_wip_pause_needs_the_dirty_paths(self) -> None:
        with pytest.raises(TransitionError, match="needs the dirty paths"):
            change_status(
                _task(),
                CompletionState.PAUSED_WITH_UNCOMMITTED_WIP,
                reason="r",
                now=NOW,
                next_action="n",
            )

    def test_wip_pause_records_every_dirty_path(self) -> None:
        task = _task()
        change_status(
            task,
            CompletionState.PAUSED_WITH_UNCOMMITTED_WIP,
            reason="r",
            now=NOW,
            next_action="n",
            dirty_paths=["b.py", "a.py"],
        )
        assert task.status_history[-1].dirty_paths == ["a.py", "b.py"]
        assert task_problems(task) == []

    def test_complete_needs_a_proof(self) -> None:
        with pytest.raises(TransitionError, match="completion proof"):
            change_status(_task(), CompletionState.COMPLETE, reason="r", now=NOW)

    def test_complete_refuses_a_proof_for_another_head(self) -> None:
        with pytest.raises(TransitionError, match="completion proof"):
            change_status(
                _task(),
                CompletionState.COMPLETE,
                reason="r",
                now=NOW,
                completion_proof=_proof(SHA_B),
            )

    def test_complete_then_stale_drops_the_proof(self) -> None:
        task = _task()
        proof = _proof()
        change_status(task, CompletionState.COMPLETE, reason="r", now=NOW, completion_proof=proof)
        assert task.completion_proof == proof
        change_status(task, CompletionState.STALE, reason="moved", now=NOW)
        assert task.completion_proof is None


class TestDeliverables:
    def _with_deliverable(self) -> TaskRun:
        return _task(deliverables=[Deliverable(deliverable_id="D-0001", description="d")])

    def test_states_advance(self) -> None:
        task = self._with_deliverable()
        item = set_deliverable_state(task, "D-0001", DeliverableState.VERIFIED, now=NOW)
        assert item.state is DeliverableState.VERIFIED
        assert item.history == [f"VERIFIED@{NOW}"]

    def test_moving_backwards_needs_explicit_regress(self) -> None:
        task = self._with_deliverable()
        set_deliverable_state(task, "D-0001", DeliverableState.COMMITTED, now=NOW)
        with pytest.raises(TransitionError, match="moves backwards"):
            set_deliverable_state(task, "D-0001", DeliverableState.IMPLEMENTED, now=NOW)
        item = set_deliverable_state(
            task, "D-0001", DeliverableState.IMPLEMENTED, now=NOW, allow_regress=True
        )
        assert item.state is DeliverableState.IMPLEMENTED

    def test_unknown_deliverable_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="no deliverable"):
            set_deliverable_state(_task(), "D-0009", DeliverableState.VERIFIED, now=NOW)


class TestDeliveryState:
    def test_not_required_without_push(self) -> None:
        task = _task(scope=TaskScope(requires_push=False))
        assert delivery_state(task) is DeliveryState.NOT_REQUIRED

    def test_pending_until_the_remote_has_head(self) -> None:
        assert delivery_state(_task()) is DeliveryState.PENDING

    def test_pushed_then_pr_open(self) -> None:
        task = _task()
        task.delivery.remote_head_sha = SHA_A
        assert delivery_state(task) is DeliveryState.PUSHED
        task.delivery.pr_number = 7
        task.delivery.pr_head_sha = SHA_B
        assert delivery_state(task) is DeliveryState.PUSHED
        task.delivery.pr_head_sha = SHA_A
        assert delivery_state(task) is DeliveryState.PR_OPEN


@dataclass
class _Odd:
    value: float


class TestCodec:
    def _full_task(self) -> TaskRun:
        task = _task(
            steps=[Step(step_id="S-0001", text="t", evidence_ids=["EV-0001"])],
            evidence=[
                Evidence(
                    evidence_id="EV-0001",
                    kind=EvidenceKind.COMMAND,
                    source=EvidenceSource.EXECUTED,
                    subject="smoke",
                    ok=True,
                    recorded_at=NOW,
                    head_sha=SHA_A,
                    tree_clean=True,
                    command=["true"],
                    exit_code=0,
                )
            ],
            checkpoints=[
                Checkpoint(
                    checkpoint_id="CP-0001",
                    created_at=NOW,
                    reason="baseline",
                    phase=TaskPhase.CREATED,
                    status=CompletionState.ACTIVE,
                    git=_snapshot([StatusEntry("renamed", "R.", "new", "old")]),
                    open_steps=["S-0001"],
                    next_action=None,
                    findings=[],
                    rejected_approaches=[],
                )
            ],
        )
        return task

    def test_a_manifest_round_trips_through_json(self) -> None:
        task = self._full_task()
        assert load_task(json.loads(json.dumps(to_dict(task)))) == task

    def test_an_unknown_field_is_refused(self) -> None:
        data = to_dict(_task())
        data["surprise"] = 1
        with pytest.raises(ManifestError, match="unknown field"):
            load_task(data)

    def test_a_missing_required_field_is_refused(self) -> None:
        data = to_dict(_task())
        del data["branch"]
        with pytest.raises(ManifestError, match="missing required field 'branch'"):
            load_task(data)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("stop_blocks", True, "expected int, got bool"),
            ("objective", 3, "expected str, got int"),
            ("status", "DONE", "not a valid CompletionState"),
            ("steps", {}, "expected a list"),
            ("completion_proof", [], "expected an object"),
            ("scope", "all", "expected an object"),
        ],
    )
    def test_a_wrong_type_is_refused(self, field: str, value: object, message: str) -> None:
        data = to_dict(_task())
        data[field] = value
        with pytest.raises(ManifestError, match=message):
            load_task(data)

    def test_a_bool_field_refuses_an_int(self) -> None:
        data = to_dict(_task())
        data["scope"]["requires_push"] = 1
        with pytest.raises(ManifestError, match="expected bool"):
            load_task(data)

    def test_optional_fields_accept_null(self) -> None:
        data = to_dict(_task())
        data["next_action"] = None
        assert load_task(data).next_action is None

    def test_an_unsupported_field_type_is_refused_not_coerced(self) -> None:
        with pytest.raises(ManifestError, match="unsupported field type"):
            from_dict(_Odd, {"value": 1.5})


class TestProblems:
    def test_a_valid_task_has_none(self) -> None:
        assert task_problems(_task()) == []

    def test_every_contract_breach_is_named(self) -> None:
        task = _task(
            schema_version=99,
            task_id="nope",
            objective=" ",
            baseline_sha="short",
            steps=[
                Step(step_id="S-0001", text="a", evidence_ids=["EV-9"]),
                Step(step_id="S-0001", text="b", state=StepState.DONE),
            ],
            completion_proof={"complete": True},
            stop_blocks=-1,
        )
        problems = " | ".join(task_problems(task))
        for expected in (
            "schema_version 99",
            "does not match TB-RUN",
            "objective is empty",
            "baseline_sha is not",
            "duplicate step id 'S-0001'",
            "cites unknown evidence 'EV-9'",
            "done_at disagrees",
            "completion_proof present while status is ACTIVE",
            "stop_blocks is negative",
        ):
            assert expected in problems

    def test_complete_without_matching_proof_is_a_problem(self) -> None:
        task = _task(status=CompletionState.COMPLETE, completion_proof=_proof(SHA_B))
        assert "COMPLETE without a completion proof" in " ".join(task_problems(task))

    def test_wip_without_its_dirty_paths_is_a_problem(self) -> None:
        task = _task(status=CompletionState.PAUSED_WITH_UNCOMMITTED_WIP)
        assert "without the dirty paths" in " ".join(task_problems(task))

    def test_load_refuses_a_task_with_problems(self) -> None:
        with pytest.raises(ManifestError, match="objective is empty"):
            load_task(to_dict(_task(objective="")))


def _entry(name: str) -> BranchAuditEntry:
    return BranchAuditEntry(
        branch_name=name,
        remote_ref=None,
        audit_id="BA-1",
        audited_head_sha=SHA_A,
        audit_started_at=NOW,
        audit_completed_at=NOW,
        commit_history_status="MERGED",
        working_tree_status="CLEAN",
        untracked_status="NONE",
        index_status="CLEAN",
        merge_state="NONE",
        rebase_state="NONE",
        related_pr=None,
        related_issue=None,
        finding="",
        action_taken="none",
        verification_status="VERIFIED",
        completion_status=BranchState.AUDITED_CLEAN,
    )


class TestLedgerAndHelpers:
    def test_ledger_round_trip_and_lookup(self) -> None:
        ledger = BranchLedger(schema_version=1, entries=[_entry("a"), _entry("b")])
        loaded = load_ledger(json.loads(json.dumps(to_dict(ledger))))
        assert loaded == ledger
        assert loaded.get("b") == _entry("b")
        assert loaded.get("c") is None

    def test_a_ledger_with_duplicate_branches_is_refused(self) -> None:
        ledger = BranchLedger(schema_version=1, entries=[_entry("a"), _entry("a")])
        with pytest.raises(ManifestError, match="duplicate entries"):
            load_ledger(to_dict(ledger))

    def test_next_id_counts_past_the_highest(self) -> None:
        assert next_id("S", []) == "S-0001"
        assert next_id("S", ["S-0002", "S-0010", "X-0099", "S-junk"]) == "S-0011"

    def test_snapshot_views(self) -> None:
        snapshot = _snapshot(
            [
                StatusEntry("changed", "M.", "staged.py"),
                StatusEntry("changed", ".M", "unstaged.py"),
                StatusEntry("unmerged", "UU", "conflict.py"),
                StatusEntry("untracked", "??", "new.py"),
                StatusEntry("ignored", "!!", "build/"),
            ]
        )
        assert snapshot.staged_paths == ["staged.py"]
        assert snapshot.unmerged_paths == ["conflict.py"]
        assert snapshot.untracked_paths == ["new.py"]
        assert "build/" not in snapshot.dirty_paths
        assert not snapshot.is_clean
        assert _snapshot().is_clean

    def test_step_views(self) -> None:
        task = _task(
            steps=[
                Step(step_id="S-0001", text="a"),
                Step(step_id="S-0002", text="b", state=StepState.DONE, done_at=NOW),
            ]
        )
        assert [s.step_id for s in task.open_steps] == ["S-0001"]
        assert [s.step_id for s in task.completed_steps] == ["S-0002"]

    def test_utc_now_is_second_precision_utc(self) -> None:
        stamp = utc_now()
        assert stamp.endswith("+00:00")
        assert "." not in stamp
