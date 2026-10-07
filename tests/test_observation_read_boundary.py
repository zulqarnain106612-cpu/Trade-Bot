"""Regression tests for the universal model-observation read boundary."""

from importlib import util
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK_PATH = ROOT / ".claude" / "hooks" / "pre_tool_use.py"


def load_hook():
    spec = util.spec_from_file_location("pre_tool_use_observation_test", HOOK_PATH)
    module = util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def policy():
    return {
        "observation_boundary": {
            "enabled": True,
            "max_read_lines": 30,
            "mcp_read_tool_patterns": ["read_file$", "fetch_file$"],
            "read_refusal_message": "bounded read required",
            "changed_file_candidates": {
                "enabled": True,
                "max_read_lines": 10,
            },
        }
    }


def test_broad_read_of_modified_tracked_file_is_refused(monkeypatch, tmp_path):
    hook = load_hook()
    hook.PROJECT_DIR = tmp_path

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(args, **kwargs):
        if args[1] == "ls-files":
            return Result(0)
        if args[1] == "diff":
            return Result(1)
        raise AssertionError(args)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    event = {
        "tool_name": "Read",
        "tool_input": {
            "file_path": str(tmp_path / "src" / "changed.py"),
            "start_line": 1,
            "end_line": 30,
        },
    }
    reason = hook._read_boundary_violation(event, policy())
    assert "uncommitted changes" in reason
    assert "<= 10 lines" in reason


def test_small_targeted_read_of_modified_file_remains_available(monkeypatch, tmp_path):
    hook = load_hook()
    hook.PROJECT_DIR = tmp_path

    class Result:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        if args[1] == "ls-files":
            Result.returncode = 0
        return Result()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    event = {
        "tool_name": "Read",
        "tool_input": {
            "file_path": str(tmp_path / "src" / "changed.py"),
            "start_line": 10,
            "end_line": 20,
        },
    }
    assert hook._read_boundary_violation(event, policy()) == ""


def test_mcp_file_read_is_subject_to_the_same_targeted_boundary(monkeypatch, tmp_path):
    hook = load_hook()
    hook.PROJECT_DIR = tmp_path

    class Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(args, **kwargs):
        if args[1] == "diff":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return Result()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    event = {
        "tool_name": "mcp__github__fetch_file",
        "tool_input": {"file_path": "src/changed.py", "start_line": 1, "end_line": 30},
    }
    reason = hook._read_boundary_violation(event, policy())
    assert "uncommitted changes" in reason
