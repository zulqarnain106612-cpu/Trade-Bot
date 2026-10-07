"""
GOV-065 -- ``scripts/agent_control.py`` and the documentation that describes it.

What these tests would catch:

* a subcommand whose exit code lies -- an incomplete task, unreconciled
  audit or failing validation exiting 0 -- which would let a script or hook
  treat a failed check as passed;
* a refused request (bad id, invalid transition, malformed input) surfacing
  as a traceback instead of exit 2 with a message;
* documentation naming an ``agent_control`` command that does not exist: the
  live repository's docs are checked against the command table here, so a
  rename that leaves the runbook stale fails the suite.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.agent_control import cli
from src.agent_control.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(repo, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(["--cwd", str(repo.path), *args], out=out)
    return code, out.getvalue()


def _create(repo, *extra: str) -> str:
    code, out = _run(
        repo,
        "task",
        "create",
        "--objective",
        "cli demo",
        "--deliverable",
        "feature::feature.txt",
        "--validation",
        "smoke=true",
        "--step",
        "write it",
        *extra,
    )
    assert code == 0, out
    return out.strip()


class TestTaskCommands:
    def test_create_show_list_and_validate(self, repo) -> None:
        task_id = _create(repo, "--no-push", "--path", "src/**")
        code, out = _run(repo, "task", "show")
        view = json.loads(out)
        assert code == 0 and view["task_id"] == task_id
        assert view["open_steps"] == ["S-0001"] and view["completed_steps"] == []
        assert view["delivery_state"] == "NOT_REQUIRED"
        assert view["scope"]["in_scope_paths"] == ["src/**"]
        assert view["validations"] == [{"name": "smoke", "command": ["true"]}]
        code, out = _run(repo, "task", "list")
        assert out.startswith(f"{task_id}\tACTIVE\tCREATED\tfeature\tcli demo")
        assert _run(repo, "task", "validate") == (0, f"{task_id} manifest valid\n")

    def test_scope_file_and_flags_combine(self, repo, tmp_path: Path) -> None:
        spec = {
            "objective": "from file",
            "deliverables": [{"description": "doc", "paths": ["d.md"]}],
            "validations": [{"name": "ci"}],
            "steps": ["first"],
            "scope": {"requires_pr": False},
        }
        path = tmp_path / "scope.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        flags = ("--pr", "--branch-audit", "--no-commit")
        code, out = _run(repo, "task", "create", "--scope-file", str(path), *flags)
        assert code == 0
        view = json.loads(_run(repo, "task", "show", out.strip())[1])
        assert view["objective"] == "from file"
        assert view["scope"] == {
            "in_scope_paths": [],
            "requires_commit": False,
            "requires_push": False,
            "requires_pr": True,
            "requires_branch_audit": True,
        }
        assert [d["paths"] for d in view["deliverables"]] == [["d.md"]]
        assert [s["text"] for s in view["steps"]] == ["first"]

    def test_phase_stop_resume_and_next(self, repo) -> None:
        task_id = _create(repo)
        assert _run(repo, "task", "phase", "PLANNING")[0] == 0
        assert _run(repo, "task", "next", "write the docs")[0] == 0
        assert _run(repo, "task", "pause", "--reason", "r", "--next-action", "n") == (
            0,
            f"{task_id} PAUSED\n",
        )
        code, out = _run(repo, "task", "resume")
        assert code == 0 and f"ACTIVE TASK: {task_id}" in out
        assert _run(repo, "task", "block", "--reason", "r", "--next-action", "n")[0] == 0
        assert _run(repo, "task", "resume")[0] == 0
        assert _run(repo, "task", "fail", "--reason", "broken")[0] == 0
        assert _run(repo, "task", "abandon", task_id, "--reason", "superseded") == (
            0,
            f"{task_id} ABANDONED\n",
        )

    def test_complete_exits_by_the_verdict(self, repo) -> None:
        _create(repo)
        code, out = _run(repo, "task", "complete", "--offline")
        assert code == 1 and json.loads(out)["complete"] is False
        assert _run(repo, "verify-completion", "--offline")[0] == 1


class TestChecklistAndEvidence:
    def test_steps_deliverables_notes_and_checkpoints(self, repo) -> None:
        _create(repo)
        assert _run(repo, "step", "add", "review") == (0, "S-0002 OPEN review\n")
        assert _run(repo, "step", "done", "S-0002") == (0, "S-0002 DONE review\n")
        assert _run(repo, "deliverable", "add", "docs", "--path", "d.md") == (
            0,
            "D-0002 REQUESTED docs\n",
        )
        verified = _run(repo, "deliverable", "set", "D-0002", "VERIFIED")
        assert verified == (0, "D-0002 VERIFIED docs\n")
        assert _run(repo, "deliverable", "set", "D-0002", "REQUESTED", "--regress")[0] == 0
        assert _run(repo, "note", "--kind", "finding", "it works") == (0, "")
        code, out = _run(repo, "checkpoint", "--reason", "mid-way", "--verify")
        assert code == 0 and out.startswith("CP-0002 ") and out.endswith("mid-way\n")
        code, out = _run(repo, "checkpoint")
        assert code == 0 and out.endswith("manual checkpoint\n")

    def test_a_checkpoint_that_does_not_read_back_is_an_error(
        self, repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _create(repo)
        monkeypatch.setattr(Workspace, "verify_checkpoint", lambda self, task_id: None)
        assert _run(repo, "checkpoint", "--verify")[0] == 2
        assert "did not read back intact" in capsys.readouterr().err

    def test_evidence_exit_codes(self, repo) -> None:
        _create(repo)
        assert _run(repo, "evidence", "run", "smoke") == (0, "EV-0001 ok smoke\n")
        repo.write("dirty.txt")
        assert _run(repo, "evidence", "run", "smoke") == (1, "EV-0002 FAILED smoke\n")
        code, out = _run(
            repo,
            "evidence",
            "ci",
            "smoke",
            "--state",
            "success",
            "--head-sha",
            "3" * 40,
            "--source",
            "https://example.invalid/pr/1#notice",
        )
        assert (code, out) == (0, "EV-0003 ok smoke\n")


class TestGitCommands:
    def test_snapshot_authorize_commit_audit_push_and_final_audit(self, repo) -> None:
        task_id = _create(repo)
        code, out = _run(repo, "git", "snapshot")
        assert code == 0 and json.loads(out)["branch"] == "feature"
        code, out = _run(repo, "git", "snapshot", task_id)
        assert code == 0 and len(Workspace.open(repo.path).load(None).checkpoints) == 2
        code, out = _run(repo, "git", "authorize", "--operation", "stash", "--reason", "r")
        assert code == 0 and out.startswith("AU-0001 granted for one 'stash'")
        head = repo.commit("feature.txt", "f\n")
        expected = (0, f"{head} verified (branch head matched: True)\n")
        assert _run(repo, "git", "verify-commit") == expected
        assert _run(repo, "git", "verify-commit", "--rev", "HEAD~1")[0] == 1
        code, out = _run(repo, "git", "audit-diff")
        assert code == 0 and "1 changed path(s):\nfeature.txt" in out
        assert _run(repo, "git", "verify-push")[0] == 1
        repo.push()
        assert _run(repo, "git", "verify-push")[0] == 0
        code, out = _run(repo, "git", "final-audit")
        report = json.loads(out)
        assert code == 0 and report["ok"] and all(report["checks"].values())
        repo.write("stray.txt")
        code, out = _run(repo, "git", "final-audit")
        assert code == 1 and json.loads(out)["checks"]["tree_clean"] is False
        repo.write("bad.txt", "x \n")
        repo.git.run("add", "bad.txt")
        repo.git.run("commit", "-q", "-m", "ws")
        assert _run(repo, "git", "audit-diff")[0] == 1

    def test_delivery_pr_exit_codes(self, repo) -> None:
        _create(repo)
        head = repo.head()
        args = ("delivery", "pr", "--number", "3", "--url", "https://example.invalid/pr/3")
        assert _run(repo, *args, "--head-sha", head)[0] == 0
        assert _run(repo, *args, "--head-sha", "4" * 40)[0] == 1


class TestBranchAuditCommands:
    def test_inventory_run_record_verify(self, repo) -> None:
        code, out = _run(repo, "branch-audit", "inventory")
        assert code == 0
        assert {row["branch"] for row in json.loads(out)} == {"feature", "main", "origin/main"}
        assert _run(repo, "branch-audit", "verify")[0] == 1
        code, out = _run(repo, "branch-audit", "run")
        assert code == 0 and json.loads(out)["audited"] == 3
        code, out = _run(repo, "branch-audit", "record", "main", "--pr", "2", "--pr-state", "open")
        assert (code, out.split(" ")[:2]) == (0, ["main", "AUDITED_WITH_OPEN_PR"])
        code, out = _run(repo, "branch-audit", "verify")
        assert code == 0 and json.loads(out)["reconciled"] is True

    def test_recording_an_unknown_branch_is_refused(self, repo, capsys) -> None:
        assert _run(repo, "branch-audit", "record", "ghost")[0] == 2
        assert "'ghost' is not in the inventory" in capsys.readouterr().err


class TestErrors:
    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("task", "show", "TB-RUN-20261007-0042"), "no task TB-RUN-20261007-0042"),
            (("task", "create"), "an objective is required"),
            (("task", "create", "--objective", "o", "--deliverable", "::p"), "has no description"),
            (("task", "create", "--objective", "o", "--validation", "x="), "NAME or NAME=COMMAND"),
            (
                ("task", "create", "--objective", "o", "--scope-file", "/nonexistent.json"),
                "cannot read",
            ),
            (("task", "phase", "DELIVERED"), "no active task"),
        ],
    )
    def test_refusals_exit_2_with_a_message(self, repo, capsys, args, message) -> None:
        assert _run(repo, *args)[0] == 2
        assert message in capsys.readouterr().err

    def test_a_scope_file_with_unknown_keys_is_refused(self, repo, tmp_path, capsys) -> None:
        path = tmp_path / "scope.json"
        path.write_text('{"objective": "o", "extra": 1}', encoding="utf-8")
        assert _run(repo, "task", "create", "--scope-file", str(path))[0] == 2
        assert "must be an object with keys" in capsys.readouterr().err

    def test_outside_a_repository(self, tmp_path: Path, capsys) -> None:
        assert cli.main(["--cwd", str(tmp_path), "session-summary"]) == 2
        assert "agent_control: error" in capsys.readouterr().err

    def test_session_summary_with_and_without_a_task(self, repo) -> None:
        assert _run(repo, "session-summary") == (0, "no active task in this worktree\n")
        task_id = _create(repo)
        assert f"ACTIVE TASK: {task_id}" in _run(repo, "session-summary")[1]


class TestDocs:
    def test_every_documented_command_exists(self) -> None:
        documented = cli.documented_commands(REPO_ROOT)
        assert documented, "the agent execution protocol must document its commands"
        assert cli.docs_problems(REPO_ROOT) == []

    def test_unknown_commands_and_subcommands_are_reported(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text(
            "python3 scripts/agent_control.py task create\n"
            "python3 scripts/agent_control.py tsk show\n"
            "python3 scripts/agent_control.py git push-it\n",
            encoding="utf-8",
        )
        assert cli.docs_problems(tmp_path) == [
            "docs/a.md: unknown command 'tsk'",
            "docs/a.md: 'git' has no subcommand 'push-it'",
        ]
        out = io.StringIO()
        assert cli.main(["--cwd", str(tmp_path), "docs", "verify"], out=out) == 1
        assert out.getvalue().endswith("3 documented invocation(s), 2 problem(s)\n")

    def test_an_undocumented_tool_fails_and_a_documented_one_passes(self, tmp_path: Path) -> None:
        out = io.StringIO()
        assert cli.main(["--cwd", str(tmp_path), "docs", "verify"], out=out) == 1
        assert "no agent_control commands are documented" in out.getvalue()
        (tmp_path / "CLAUDE.md").write_text(
            "run `python3 scripts/agent_control.py verify-completion`\n", encoding="utf-8"
        )
        assert cli.main(["--cwd", str(tmp_path), "docs", "verify"], out=io.StringIO()) == 0

    def test_the_parser_covers_the_command_table(self, capsys) -> None:
        parser = cli.build_parser()
        for command, subs in cli.COMMANDS.items():
            for sub in subs or (None,):
                argv = [command, "--help"] if sub is None else [command, sub, "--help"]
                with pytest.raises(SystemExit) as exited:
                    parser.parse_args(argv)
                assert exited.value.code == 0, argv
        assert "usage:" in capsys.readouterr().out
