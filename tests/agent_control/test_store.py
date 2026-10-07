"""
GOV-060 -- the manifest store: durable, atomic, and outside every work tree.

What these tests would catch:

* a manifest that only lives in the writing process -- a fresh store (what a
  new session or a hook process is) must read back exactly what was saved;
* a task id used as a path component without validation, which would let an
  id like ``../../x`` write outside the state directory;
* a corrupt or hand-edited file accepted instead of refused;
* an invalid task saved -- the store re-checks the contract on every write.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.agent_control.model import (
    SCHEMA_VERSION,
    BranchLedger,
    CompletionState,
    ManifestError,
    TaskPhase,
    TaskRun,
    TaskScope,
)
from src.agent_control.store import (
    STATE_DIR_ENV,
    TaskNotFoundError,
    TaskStore,
    default_state_dir,
)

SHA = "c" * 40
NOW = "2026-10-07T00:00:00+00:00"


def _task(task_id: str = "TB-RUN-20261007-0001") -> TaskRun:
    return TaskRun(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        objective="o",
        created_at=NOW,
        updated_at=NOW,
        repository="/r/.git",
        worktree="/r",
        branch="b",
        baseline_sha=SHA,
        current_sha=SHA,
        phase=TaskPhase.CREATED,
        status=CompletionState.ACTIVE,
        scope=TaskScope(),
    )


def test_state_dir_defaults_to_the_common_git_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    assert default_state_dir("/repo/.git") == Path("/repo/.git/agent-control")


def test_state_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    assert default_state_dir("/repo/.git") == tmp_path


def test_a_saved_task_reads_back_through_a_fresh_store(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    with store.locked():
        store.save(_task())
    assert TaskStore(tmp_path).load("TB-RUN-20261007-0001") == _task()
    assert not list(tmp_path.glob("tasks/.*.tmp"))


def test_an_invalid_task_is_never_written(tmp_path: Path) -> None:
    task = _task()
    task.objective = ""
    with pytest.raises(ManifestError, match="refusing to save"):
        TaskStore(tmp_path).save(task)
    assert TaskStore(tmp_path).task_ids() == []


@pytest.mark.parametrize("task_id", ["../../escape", "TB-RUN-1-1", "tb-run-20261007-0001"])
def test_a_malformed_id_is_never_a_path(tmp_path: Path, task_id: str) -> None:
    with pytest.raises(ManifestError, match="is not a task id"):
        TaskStore(tmp_path).manifest_path(task_id)


def test_a_missing_task_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TaskNotFoundError, match="no task TB-RUN-20261007-0009"):
        TaskStore(tmp_path).load("TB-RUN-20261007-0009")


def test_a_corrupt_manifest_is_refused(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.tasks_dir.mkdir(parents=True)
    (store.tasks_dir / "TB-RUN-20261007-0001.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        store.load("TB-RUN-20261007-0001")


def test_ids_are_listed_sorted_and_allocated_per_day(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    assert store.task_ids() == []
    assert store.next_task_id(date(2026, 10, 7)) == "TB-RUN-20261007-0001"
    for task_id in ("TB-RUN-20261007-0002", "TB-RUN-20261006-0005"):
        store.save(_task(task_id))
    (store.tasks_dir / "TB-RUN-notes.json").write_text("{}", encoding="utf-8")
    assert store.task_ids() == ["TB-RUN-20261006-0005", "TB-RUN-20261007-0002"]
    assert store.next_task_id(date(2026, 10, 7)) == "TB-RUN-20261007-0003"
    assert store.next_task_id(date(2026, 10, 8)) == "TB-RUN-20261008-0001"


def test_the_active_pointer_is_per_worktree(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    assert store.active_task_id("/a") is None
    store.set_active("/a", "TB-RUN-20261007-0001")
    store.set_active("/b", "TB-RUN-20261007-0002")
    assert TaskStore(tmp_path).active_task_id("/a") == "TB-RUN-20261007-0001"
    store.set_active("/a", None)
    assert store.active_task_id("/a") is None
    assert store.active_task_id("/b") == "TB-RUN-20261007-0002"


@pytest.mark.parametrize("content", [[1, 2], {"/a": 3}])
def test_a_malformed_active_pointer_is_refused(tmp_path: Path, content: object) -> None:
    (tmp_path / "active.json").write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ManifestError, match="must map worktree paths"):
        TaskStore(tmp_path).active_task_id("/a")


def test_the_ledger_starts_empty_and_round_trips(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    assert store.load_ledger() == BranchLedger(schema_version=SCHEMA_VERSION)
    store.save_ledger(BranchLedger(schema_version=SCHEMA_VERSION))
    assert TaskStore(tmp_path).load_ledger().entries == []


def test_the_lock_is_released_after_an_error(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    with pytest.raises(RuntimeError), store.locked():
        raise RuntimeError("boom")
    with store.locked():
        store.save(_task())
    assert store.task_ids() == ["TB-RUN-20261007-0001"]
