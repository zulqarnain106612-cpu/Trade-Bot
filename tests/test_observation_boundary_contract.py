"""Permanent contract for the universal observation boundary.

Decides:
<<<<<<< HEAD
  - REG-0024 — local checks are CI-only and the prepare route exposes only the current HEAD notice
=======
  - REG-0024 — local checks are CI-only; prfail is the report route and the patcher is SHA-bound
>>>>>>> origin/main
  - GOV-057 — every model-visible tool observation crosses the repository boundary
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE = ROOT / ".claude" / "hooks" / "pre_tool_use.py"
GATE = ROOT / ".claude" / "hooks" / "observation_gate.py"
CONFIG = ROOT / "config" / "observation_boundary.json"
SETTINGS = ROOT / ".claude" / "settings.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_settings_route_every_success_and_failure_through_universal_hooks():
    settings = json.loads(SETTINGS.read_text())
    assert any(
        e.get("matcher") == "*" and any("pre_tool_use.py" in h["command"] for h in e["hooks"])
        for e in settings["hooks"]["PreToolUse"]
    )
    assert any(
        e.get("matcher") == "*" and any("observation_gate.py" in h["command"] for h in e["hooks"])
        for e in settings["hooks"]["PostToolUse"]
    )
    assert any(
        e.get("matcher") == "*"
        and any("observation_failure.py" in h["command"] for h in e["hooks"])
        for e in settings["hooks"]["PostToolUseFailure"]
    )


def test_read_is_hard_bounded_to_eighty_lines():
    pre = load_module(PRE, "pre_boundary_contract")
    event = {"tool_name": "Read", "tool_input": {"file_path": "src/example.py"}}
    assert pre._read_boundary_violation(event, {}) is not None
    assert (
        pre._read_boundary_violation(
            {"tool_name": "Read", "tool_input": {"file_path": "src/example.py", "limit": 80}},
            {},
        )
        == ""
    )
    assert pre._read_boundary_violation(
        {"tool_name": "Read", "tool_input": {"file_path": "src/example.py", "limit": 81}},
        {},
    )


def test_policy_off_cannot_disable_observation_boundary(monkeypatch):
    pre = load_module(PRE, "pre_boundary_policy_off")
    monkeypatch.setenv("TB_COMMAND_POLICY", "off")
    event = {"tool_name": "Read", "tool_input": {"file_path": "src/example.py"}}
    assert pre._violations(event, {})  # hard read control remains


def test_documentary_enabled_flag_cannot_disable_read_boundary():
    pre = load_module(PRE, "pre_boundary_enabled_flag")
    event = {"tool_name": "Read", "tool_input": {"file_path": "src/example.py"}}
    assert pre._read_boundary_violation(
        event,
        {"observation_boundary": {"enabled": False, "max_read_lines": 1000}},
    )


def test_diagnostics_control_plane_paths_are_protected_in_config_and_hook():
    pre = load_module(PRE, "pre_boundary_diagnostics_paths")
    config = json.loads(CONFIG.read_text())
    configured = set(config["protected_boundary_paths"])
    for path in (
        ".github/workflows",
        ".github/CODEOWNERS",
        "common/command_schema.py",
        "common/shell_exec.py",
        "scripts/ci_diagnostics.py",
        "scripts/prfail",
        "scripts/apply_ci_patch.py",
        "tests/test_ci_diagnostics.py",
        "tests/test_apply_ci_patch.py",
    ):
        assert path in configured
        assert path in pre.PROTECTED_BOUNDARY_PATHS


def test_protected_boundary_files_cannot_be_written_by_native_tools():
    pre = load_module(PRE, "pre_boundary_native_write")
    reason = pre._protected_boundary_violation(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": ".claude/hooks/observation_gate.py",
                "content": "bypass",
            },
        }
    )
    assert reason and "Protected observation-boundary" in reason


def test_protected_boundary_files_cannot_be_mutated_through_github_file_tool():
    pre = load_module(PRE, "pre_boundary_github_write")
    reason = pre._protected_boundary_violation(
        {
            "tool_name": "mcp__GitHub__update_file",
            "tool_input": {
                "path": ".claude/hooks/observation_gate.py",
                "sha": "abc",
                "content": "bypass",
            },
        }
    )
    assert reason and "Protected observation-boundary" in reason


def test_protected_boundary_files_cannot_be_mutated_with_shell_redirection():
    pre = load_module(PRE, "pre_boundary_bash_write")
    reason = pre._protected_boundary_violation(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": 'printf "bypass\n" > .claude/hooks/observation_gate.py',
            },
        }
    )
    assert reason and "Protected observation-boundary" in reason


def test_changed_source_is_diff_first():
    gate = load_module(GATE, "gate_boundary_contract")
    fake_diff = "diff --git a/src/example.py b/src/example.py\n@@ -1,3 +1,3 @@\n-old()\n+new()\n"
    original = gate._git_diff_for_path
    gate._git_diff_for_path = lambda _: fake_diff
    try:
        event = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(ROOT / "src" / "example.py"), "limit": 80},
            "tool_response": "\n".join(f"{i}\\told source" for i in range(200)),
        }
        output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
        assert "diff-first" in output
        assert "+new()" in output
        assert "old source" not in output
    finally:
        gate._git_diff_for_path = original


def test_boundary_error_withholds_raw_output(monkeypatch):
    gate = load_module(GATE, "gate_boundary_error")
    original = gate.tool_name
    gate.tool_name = lambda _event: (_ for _ in ()).throw(RuntimeError("boom"))
    output = io.StringIO()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Read", "tool_response": "SECRET_RAW"})),
    )
    monkeypatch.setattr(sys, "stdout", output)
    try:
        gate.main()
    finally:
        gate.tool_name = original

    replacement = json.loads(output.getvalue())["hookSpecificOutput"]["updatedToolOutput"]
    assert "SECRET_RAW" not in replacement
    assert "observation withheld" in replacement


def test_default_deny_route_gate_is_code_owned_and_independent_of_policy_mode():
    pre = load_module(PRE, "pre_boundary_default_deny")
    for tool in (
        "Bash",
        "Read",
        "mcp__Desktop_Commander__start_process",
        "mcp__unknown__read_file",
        "mcp__github__new_future_tool",
        "mcp__github__fetch_pr_comments",
        "mcp__github__fetch_pr",
        "mcp__github__list_pull_request_reviews",
        "mcp__github__list_pull_request_review_threads",
    ):
        reason = pre._github_route_violation({"tool_name": tool, "tool_input": {}})
        assert reason and "denied" in reason.lower()


<<<<<<< HEAD
def test_route_gate_allows_only_the_exact_pr_notice_subscription():
=======
def test_route_gate_allows_only_the_exact_pr_report_command():
>>>>>>> origin/main
    pre = load_module(PRE, "pre_boundary_notice_subscription")
    assert (
        pre._github_route_violation(
            {
                "tool_name": "Bash",
<<<<<<< HEAD
                "tool_input": {"command": "python3 scripts/local_checks.py prepare"},
=======
                "tool_input": {"command": "./scripts/prfail --pr 458"},
>>>>>>> origin/main
            }
        )
        == ""
    )
    for command in (
        "gh pr view 439 --json comments",
        "cd /repo && python3 scripts/local_checks.py prepare && gh pr view 439 --json comments",
        "python3 scripts/local_checks.py run tests",
    ):
        reason = pre._github_route_violation(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert reason and "denied" in reason.lower(), command


def test_route_gate_allows_only_scoped_github_operations():
    pre = load_module(PRE, "pre_boundary_github_only")
    assert (
        pre._github_route_violation(
            {
                "tool_name": "mcp__github__get_file_contents",
                "tool_input": {
                    "owner": "zulqarnain106612-cpu",
                    "repo": "Trade-Bot",
                    "path": "CLAUDE.md",
                    "ref": "main",
                },
            }
        )
        == ""
    )
    assert pre._github_route_violation(
        {
            "tool_name": "mcp__github__get_file_contents",
            "tool_input": {"owner": "other", "repo": "repo", "path": "README.md", "ref": "main"},
        }
    )


def test_route_gate_denies_generic_github_fetch_of_ci_data():
    pre = load_module(PRE, "pre_boundary_github_ci")
    reason = pre._github_route_violation(
        {
            # Carried on an allowed tool: the CI-URL guard must fire even when
            # the tool name itself is contracted.
            "tool_name": "mcp__github__get_file_contents",
            "tool_input": {
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
                "path": "CLAUDE.md",
                "ref": "main",
                "url": "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/actions/runs/1",
            },
        }
    )
    assert reason and "CI" in reason


def test_github_only_policy_blocks_all_local_checks_and_allows_notice_preparation():
    pre = load_module(PRE, "pre_boundary_github_only_checks")
    policy = json.loads((ROOT / "config" / "command_policy.json").read_text(encoding="utf-8"))

    blocked = (
        "TB_LOCAL_CHECKS=1 python3 scripts/local_checks.py run tests",
        "pytest -q tests/test_example.py",
        "python3 tests/test_example.py",
        "python3 -m unittest",
        "python3 -m build",
        "cargo test",
        "make test",
        "python3 scripts/run_tests.py",
        "docker compose build",
        "git diff --check",
        "npm run typecheck",
        "uv run pytest tests/test_example.py",
        "cargo fmt --check",
        "ruff check .",
        "npm run build",
        "python3 .claude/skills/quality-engineering/scripts/qe_gate.py",
    )
    for command in blocked:
        reason = pre._local_check_violation(command, policy)
        assert reason and "disabled" in reason.lower(), command

<<<<<<< HEAD
    assert pre._local_check_violation("python3 scripts/local_checks.py prepare", policy) == ""
=======
    assert pre._local_check_violation("python3 scripts/local_checks.py prepare", policy)
>>>>>>> origin/main
    assert pre._local_check_violation("git status --short", policy) == ""


def test_config_carries_single_universal_policy():
    config = json.loads(CONFIG.read_text())
    assert config["enabled"] is True
    assert config["max_read_lines"] == 80
    assert config["policy"]["changed_local_source"] == "diff_first"
    assert config["github_only_route"]["enabled"] is True
    assert config["github_only_route"]["unknown_tool_policy"] == "deny"
    assert config["github_only_route"]["repository"] == "zulqarnain106612-cpu/Trade-Bot"
