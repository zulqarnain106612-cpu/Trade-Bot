"""
GOV-063 / GOV-064 -- the lifecycle hooks.

What these tests would catch:

* a turn ending while the active task is incomplete, with no blocker named;
* the opposite trap: a session that can never end because the gate blocks
  forever (it must force an honest PAUSE after MAX_STOP_BLOCKS);
* compaction proceeding although the checkpoint was not persisted;
* a new session starting without the task state that was on disk;
* any hook acting when no task is active -- sessions that do not use the
  protocol must be unaffected -- or crashing instead of failing open.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.agent_control import hooks
from src.agent_control.model import CompletionState, TaskScope
from src.agent_control.workspace import Workspace


def _event(repo, **extra: object) -> dict[str, object]:
    return {"cwd": str(repo.path), **extra}


def _complete_without_push(repo) -> str:
    ws = repo.workspace()
    task = ws.create_task("small", scope=TaskScope(requires_push=False))
    repo.commit("done.txt", "done\n")
    ws.verify_commit(task.task_id)
    ws.audit_diff(task.task_id)
    return task.task_id


class TestStop:
    def test_no_repository_or_no_task_allows_the_stop(self, repo, tmp_path: Path) -> None:
        assert hooks.stop({"cwd": str(tmp_path)}) is None
        assert hooks.stop(_event(repo)) is None

    def test_an_incomplete_task_blocks_with_its_predicates(self, repo, make_task) -> None:
        task = make_task()
        result = hooks.stop(_event(repo))
        assert result is not None and result["decision"] == "block"
        assert f"Task {task.task_id} is ACTIVE and not complete" in result["reason"]
        assert "- open checklist item(s): S-0001 'write the feature'" in result["reason"]
        assert f"task pause {task.task_id}" in result["reason"]
        assert repo.workspace().load(None).stop_blocks == 1

    def test_a_task_that_passes_is_recorded_complete(self, repo) -> None:
        _complete_without_push(repo)
        assert hooks.stop(_event(repo)) is None
        assert repo.workspace().load(None).status is CompletionState.COMPLETE

    def test_an_explicitly_stopped_task_does_not_block(self, repo, make_task) -> None:
        make_task()
        repo.workspace().stop(None, CompletionState.BLOCKED, reason="r", next_action="n")
        assert hooks.stop(_event(repo)) is None

    def test_repeated_blocks_end_in_an_honest_pause(
        self, repo, make_task, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_task()
        monkeypatch.setattr(hooks, "MAX_STOP_BLOCKS", 1)
        repo.write("wip.txt")
        assert hooks.stop(_event(repo)) is not None
        assert hooks.stop(_event(repo)) is None
        task = repo.workspace().load(None)
        assert task.status is CompletionState.PAUSED_WITH_UNCOMMITTED_WIP
        assert task.status_history[-1].reason.startswith("stop forced after 1 completion-gate")
        assert task.next_action is not None and task.next_action.startswith("resolve: ")


class TestPreCompact:
    def test_nothing_to_persist_allows_compaction(self, repo, tmp_path: Path) -> None:
        assert hooks.precompact({"cwd": str(tmp_path)}) is None
        assert hooks.precompact(_event(repo)) is None

    def test_the_checkpoint_is_persisted_and_read_back(self, repo, make_task) -> None:
        make_task()
        assert hooks.precompact(_event(repo, trigger="auto")) is None
        assert repo.workspace().load(None).checkpoints[-1].reason == "pre-compact (auto)"

    def test_a_checkpoint_that_does_not_read_back_blocks(
        self, repo, make_task, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = make_task()
        stale = task.checkpoints[0]
        monkeypatch.setattr(Workspace, "verify_checkpoint", lambda self, task_id: stale)
        result = hooks.precompact(_event(repo, trigger="manual"))
        assert result is not None and result["decision"] == "block"
        assert "not on disk when re-read" in result["reason"]

    def test_a_store_that_cannot_be_written_blocks(
        self, repo, make_task, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_task()

        def broken(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Workspace, "checkpoint", broken)
        result = hooks.precompact(_event(repo))
        assert result is not None
        assert "OSError: disk full" in result["reason"]
        assert "Compaction is blocked" in result["reason"]


class TestSessionStart:
    def test_nothing_is_said_without_a_task(self, repo, tmp_path: Path) -> None:
        assert hooks.session_start({"cwd": str(tmp_path)}) == ""
        assert hooks.session_start(_event(repo)) == ""

    def test_the_recovery_summary_is_rebuilt_from_disk(self, repo, make_task) -> None:
        task = make_task()
        text = hooks.session_start(_event(repo, source="compact"))
        assert text.startswith("[agent-control] task state recovered from disk (session compact)")
        assert f"ACTIVE TASK: {task.task_id}" in text


class TestPostBash:
    @pytest.mark.parametrize(
        "command",
        [None, "ls", "echo commit", "git commit -m 'unterminated", "git status # commit"],
    )
    def test_commands_that_are_not_commits_are_ignored(self, repo, make_task, command) -> None:
        make_task()
        hooks.post_bash(_event(repo, tool_input={"command": command}))
        assert repo.workspace().load(None).commits == []

    def test_a_commit_is_verified_against_the_active_task(self, repo, make_task) -> None:
        make_task()
        head = repo.commit("feature.txt")
        hooks.post_bash(_event(repo, tool_input={"command": "git commit -m feature"}))
        assert [c.sha for c in repo.workspace().load(None).commits] == [head]

    def test_no_task_or_a_stopped_task_records_nothing(self, repo, make_task, tmp_path) -> None:
        hooks.post_bash({"cwd": str(tmp_path), "tool_input": {"command": "git commit -m x"}})
        hooks.post_bash(_event(repo, tool_input={"command": "git commit -m x"}))
        make_task()
        repo.workspace().stop(None, CompletionState.FAILED, reason="r")
        repo.commit("x.txt")
        hooks.post_bash(_event(repo, tool_input={"command": "git commit -m x"}))
        assert repo.workspace().load(None).commits == []


class TestGitGuard:
    def test_only_guarded_git_commands_in_a_task_are_refused(
        self, repo, make_task, tmp_path: Path
    ) -> None:
        checkout = {"command": "git checkout main"}
        assert hooks.git_guard(_event(repo, tool_input={"command": "ls"})) is None
        assert hooks.git_guard({"cwd": str(tmp_path), "tool_input": checkout}) is None
        assert hooks.git_guard(_event(repo, tool_input=checkout)) is None
        task = make_task()
        message = hooks.git_guard(_event(repo, tool_input=checkout))
        assert message is not None
        assert f"git authorize {task.task_id} --operation checkout" in message
        repo.workspace().authorize(None, "checkout", "r")
        assert hooks.git_guard(_event(repo, tool_input=checkout)) is None
        assert hooks.git_guard(_event(repo, tool_input={"command": 7})) is None


class TestRunHook:
    def _run(self, name: str, payload: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook(name, io.StringIO(payload), out, err)
        return code, out.getvalue(), err.getvalue()

    @pytest.mark.parametrize(
        ("name", "payload", "complaint"),
        [
            ("stop", "{not json", "JSONDecodeError"),
            ("stop", "[1]", "not a JSON object"),
            ("nonsense", "{}", "unknown hook"),
        ],
    )
    def test_bad_input_fails_open(self, name: str, payload: str, complaint: str) -> None:
        code, out, err = self._run(name, payload)
        assert (code, out) == (0, "")
        assert "degraded, allowing" in err and complaint in err

    def test_each_hook_answers_in_its_own_shape(self, repo, make_task) -> None:
        payload = json.dumps(_event(repo))
        assert self._run("stop", payload) == (0, "", "")
        task = make_task()
        code, out, _ = self._run("stop", payload)
        assert code == 0 and json.loads(out)["decision"] == "block"
        assert self._run("precompact", payload) == (0, "", "")
        code, out, _ = self._run("session-start", payload)
        assert f"ACTIVE TASK: {task.task_id}" in out
        commit = json.dumps(_event(repo, tool_input={"command": "git commit -m x"}))
        assert self._run("post-bash", commit) == (0, "", "")

    def test_an_empty_payload_is_an_empty_event(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert self._run("session-start", "") == (0, "", "")
