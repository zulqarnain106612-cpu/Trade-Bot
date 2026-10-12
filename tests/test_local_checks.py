"""
REG-0024 — local test/build/check execution is disabled; only the current
HEAD's single SHA-bound CI notice may be read through prepare.

Decides:
  - REG-0024 — the agent receives only the exact new notice and cannot run checks locally
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_checks.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_checks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ci_notice_paths_are_narrow():
    module = load_module()
    comment = """
    <!-- ci-notice -->
    <!-- ci-notice-sha:abc123 -->
    **CI / Python tests (shard 3/6)** — failure
    FAILED tests/test_event_loop_acquisition.py::test_no_source_file_calls_get_event_loop
    """
    assert module.test_paths(comment) == ["tests/test_event_loop_acquisition.py"]


def test_current_pr_requires_the_exact_open_head_sha(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        module,
        "api",
        lambda path: [
            {"state": "open", "head": {"sha": "older"}, "number": 11},
            {"state": "closed", "head": {"sha": "abc123"}, "number": 12},
            {"state": "open", "head": {"sha": "abc123"}, "number": 13},
        ],
    )
    assert module.current_pr("abc123")["number"] == 13


def test_latest_notice_returns_only_the_current_heads_notice(monkeypatch):
    module = load_module()
    comments = [
        {
            "user": {"login": "github-actions[bot]"},
            "updated_at": "2026-10-12T10:00:00Z",
            "body": "<!-- ci-notice -->\n<!-- ci-notice-sha:older -->\n**stale failure**",
        },
        {
            "user": {"login": "someone-else"},
            "updated_at": "2026-10-12T12:00:00Z",
            "body": "<!-- ci-notice -->\n<!-- ci-notice-sha:abc123 -->\nspoofed user comment",
        },
        {
            "user": {"login": "github-actions[bot]"},
            "updated_at": "2026-10-12T11:00:00Z",
            "body": "<!-- ci-notice -->\n<!-- ci-notice-sha:abc123 -->\nworkflow Succeeded",
        },
    ]
    monkeypatch.setattr(module, "api", lambda path: comments)
    assert module.latest_notice(123, "abc123") == comments[-1]["body"]


def test_prepare_green_deletes_any_stale_plan(tmp_path, monkeypatch, capsys):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    module.PLAN.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    monkeypatch.setattr(
        module,
        "latest_notice",
        lambda pr, sha: "<!-- ci-notice -->\n<!-- ci-notice-sha:abc123 -->\nworkflow Succeeded",
    )
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    from src.agent_control import reliability

    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert module.prepare() == 0
    assert not module.PLAN.exists()
    assert capsys.readouterr().out.strip() == "workflow Succeeded"


def test_prepare_records_only_the_single_failed_check(tmp_path, monkeypatch, capsys):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    notice = (
        "<!-- ci-notice -->\n"
        "<!-- ci-notice-sha:abc123 -->\n"
        "**CI / Python tests (shard 1/6)** — failure · Run tests\n"
        "FAILED tests/test_event_loop_acquisition.py::test_no_source"
    )
    monkeypatch.setattr(module, "latest_notice", lambda pr, sha: notice)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    from src.agent_control import reliability

    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert module.prepare() == 0
    plan = json.loads(module.PLAN.read_text(encoding="utf-8"))
    assert plan["failed_checks"] == ["CI / Python tests (shard 1/6)"]
    assert plan["test_paths"] == ["tests/test_event_loop_acquisition.py"]
    assert plan["notice"] == notice
    assert capsys.readouterr().out == (
        "**CI / Python tests (shard 1/6)** — failure · Run tests\n"
        "FAILED tests/test_event_loop_acquisition.py::test_no_source\n"
    )


def test_shell_exec_refuses_local_test_and_build_commands():
    from common.shell_exec import run

    for command in (
        "pytest -q tests/test_example.py",
        "python3 tests/test_example.py",
        "python3 -m unittest",
        "npm run build",
        "ruff check .",
    ):
        result = run({"command": command, "output_policy": {"max_lines": 1}})
        assert result["exit_code"] == -1, command
        assert result["attempt_count"] == 0, command


def test_policy_has_one_execution_path_and_one_comment_subscription():
    from common.command_schema import LOCAL_CHECK_PATTERNS

    policy = json.loads((ROOT / "config" / "command_policy.json").read_text(encoding="utf-8"))
    local = policy["local_checks"]
    assert local["github_only"] is True
    assert local["run_command"].startswith("DISABLED:")
    assert tuple(local["blocked_patterns"]) == LOCAL_CHECK_PATTERNS
    assert local["prepare_command"] == "python3 scripts/local_checks.py prepare"


def test_cli_run_route_is_disabled_even_if_a_failure_was_recorded():
    module = load_module()
    try:
        module.main(["run"])
    except SystemExit as exc:
        assert "GitHub Actions is the sole" in str(exc)
    else:
        raise AssertionError("local check execution must remain disabled")


def test_the_script_can_import_the_repository_package(monkeypatch):
    load_module()
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != ROOT])
    load_module()
    assert str(ROOT) in sys.path
