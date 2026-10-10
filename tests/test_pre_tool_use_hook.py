"""Contract tests for the universal GitHub-only PreToolUse boundary.

Decides:
  - GOV-011 — source inspection stays bounded by the observation hook
  - GOV-012 — live CI monitoring is refused
  - GOV-019 — CI run data is unreadable by every tool route
  - GOV-057 — every authorized tool observation crosses the repository boundary
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
HOOK = PROJECT_DIR / ".claude" / "hooks" / "pre_tool_use.py"
REPO = "zulqarnain106612-cpu/Trade-Bot"


def decide(tool: str, tool_input: dict, env_extra: dict | None = None) -> dict:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(PROJECT_DIR)}
    env.pop("TB_COMMAND_POLICY", None)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    return json.loads(proc.stdout)["hookSpecificOutput"]


def assert_denied(tool: str, tool_input: dict | None = None, *, contains: str = "") -> None:
    decision = decide(tool, tool_input or {})
    assert decision["permissionDecision"] == "deny"
    if contains:
        assert contains.lower() in decision["permissionDecisionReason"].lower()


class TestDefaultDeny:
    @pytest.mark.parametrize(
        "tool",
        [
            "Bash", "Read", "NotebookRead", "Glob", "Grep", "LS", "Write", "Edit",
            "MultiEdit", "NotebookEdit", "PowerShell", "Monitor", "WebFetch", "WebSearch",
            "mcp__Desktop_Commander__start_process", "mcp__Desktop_Commander__read_file",
            "mcp__remote__terminal", "mcp__unknown__read_file", "mcp__github__new_future_tool",
        ],
    )
    def test_every_non_allowlisted_route_is_denied(self, tool: str) -> None:
        assert_denied(tool, {"command": "anything"}, contains="boundary")

    @pytest.mark.parametrize("override", ["off", "warn"])
    def test_policy_overrides_cannot_reopen_native_routes(self, override: str) -> None:
        decision = decide("Bash", {"command": "printf hello"}, {"TB_COMMAND_POLICY": override})
        assert decision["permissionDecision"] == "deny"
        assert "GitHub-only" in decision["permissionDecisionReason"]

    def test_unknown_future_github_tools_are_denied_by_default(self) -> None:
        assert_denied(
            "mcp__github__new_unreviewed_tool",
            {"owner": "zulqarnain106612-cpu", "repo": "Trade-Bot"},
        )

    def test_malformed_hook_input_fails_closed(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="{",
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(PROJECT_DIR)},
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0
        decision = json.loads(proc.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"


class TestApprovedGithubRoute:
    def test_repository_file_read_is_allowed_only_when_repo_scoped(self) -> None:
        decision = decide(
            "mcp__github__get_file_contents",
            {"owner": "zulqarnain106612-cpu", "repo": "Trade-Bot", "path": "CLAUDE.md"},
        )
        assert decision["permissionDecision"] == "allow"

    def test_connector_style_scoped_file_read_is_allowed(self) -> None:
        decision = decide(
            "mcp__GitHub__fetch_file",
            {"repository_full_name": REPO, "path": "CLAUDE.md", "start_line": 1, "end_line": 30},
        )
        assert decision["permissionDecision"] == "allow"

    def test_scoped_search_is_allowed(self) -> None:
        decision = decide(
            "mcp__github__search_code",
            {
                "query": "repo:zulqarnain106612-cpu/Trade-Bot observation_gate",
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
            },
        )
        assert decision["permissionDecision"] == "allow"

    def test_pull_request_notice_comment_route_remains_allowed(self) -> None:
        decision = decide(
            "mcp__github__fetch_pr",
            {"repository_full_name": REPO, "pr_number": 435, "include_comments": True},
        )
        assert decision["permissionDecision"] == "allow"

    def test_scoped_generic_contents_fetch_is_allowed(self) -> None:
        decision = decide(
            "mcp__GitHub__fetch",
            {
                "url": (
                    "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/"
                    "contents/CLAUDE.md"
                )
            },
        )
        assert decision["permissionDecision"] == "allow"

    def test_generic_github_endpoint_outside_the_allowlist_is_denied(self) -> None:
        assert_denied(
            "mcp__GitHub__fetch",
            {"url": "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/releases"},
            contains="endpoint is not allowlisted",
        )

    def test_search_cannot_smuggle_a_second_repository_in_the_query(self) -> None:
        assert_denied(
            "mcp__github__search_code",
            {
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
                "query": "repo:another-owner/another-repo secret",
            },
            contains=REPO,
        )

    def test_unscoped_search_is_denied(self) -> None:
        assert_denied(
            "mcp__github__search_code", {"query": "observation_gate"},
            contains="explicitly identify",
        )

    def test_other_repository_is_denied(self) -> None:
        assert_denied(
            "mcp__github__get_file_contents",
            {"owner": "another-owner", "repo": "another-repo", "path": "README.md"},
            contains=REPO,
        )

    def test_conflicting_repository_references_are_denied(self) -> None:
        assert_denied(
            "mcp__GitHub__fetch_file",
            {
                "repository_full_name": REPO,
                "url": "https://github.com/other/repo/blob/main/a.py",
                "path": "a.py",
            },
            contains=REPO,
        )


class TestCIBoundary:
    @pytest.mark.parametrize(
        "tool",
        [
            "mcp__github__get_workflow_run_logs",
            "mcp__github__fetch_workflow_job_steps",
            "mcp__GitHub__fetch_workflow_run_artifacts",
            "mcp__GitHub__get_commit_combined_status",
            "mcp__github__get_pull_request_status",
        ],
    )
    def test_ci_logs_artifacts_jobs_and_status_tools_are_denied(self, tool: str) -> None:
        assert_denied(tool, {"repository_full_name": REPO, "pull_number": 435})

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/actions/runs/1",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/check-runs/1",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/commits/abc/status",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/actions/artifacts",
        ],
    )
    def test_generic_github_fetch_cannot_read_ci_data(self, url: str) -> None:
        assert_denied("mcp__github__fetch", {"url": url}, contains="CI logs")

    def test_generic_fetch_rejects_encoded_path_traversal(self) -> None:
        assert_denied(
            "mcp__GitHub__fetch",
            {
                "url": (
                    "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/"
                    "contents/%2e%2e%2factions%2fruns"
                )
            },
            contains="endpoint is not allowlisted",
        )

    def test_pull_request_notice_comment_remains_the_ci_diagnostic_channel(self) -> None:
        assert (
            decide(
                "mcp__github__fetch_pr",
                {"repository_full_name": REPO, "pr_number": 435, "include_comments": True},
            )["permissionDecision"]
            == "allow"
        )


class TestBranchAndBoundaryWrites:
    def test_feature_branch_can_be_created_from_main_only(self) -> None:
        decision = decide(
            "mcp__github__create_branch",
            {
                "repository_full_name": REPO,
                "branch_name": "security/observation-boundary-test",
                "base_ref": "main",
            },
        )
        assert decision["permissionDecision"] == "allow"

    @pytest.mark.parametrize(
        "args",
        [
            {"branch_name": "security/other-base", "base_ref": "legacy"},
            {"branch_name": "security/arbitrary-sha", "sha": "abc123"},
            {"branch_name": "main", "base_ref": "main"},
        ],
    )
    def test_branch_creation_from_non_main_or_arbitrary_sha_is_denied(self, args: dict) -> None:
        assert_denied(
            "mcp__github__create_branch",
            {"repository_full_name": REPO, **args},
            contains="branch",
        )

    def test_pull_request_must_target_main(self) -> None:
        assert_denied(
            "mcp__github__create_pull_request",
            {
                "repository_full_name": REPO,
                "head_branch": "security/observation-boundary",
                "base_branch": "another-feature",
            },
            contains="target main",
        )

    def test_ref_update_requires_a_lease(self) -> None:
        assert_denied(
            "mcp__github__update_ref",
            {
                "repository_full_name": REPO,
                "branch_name": "security/observation-boundary",
                "sha": "abc123",
                "force": False,
            },
            contains="expected_sha lease",
        )

    def test_feature_branch_write_is_allowed(self) -> None:
        decision = decide(
            "mcp__github__push_files",
            {
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
                "branch": "security/observation-boundary",
                "files": [{"path": "docs/example.md", "content": "x"}],
            },
        )
        assert decision["permissionDecision"] == "allow"

    @pytest.mark.parametrize("branch", ["main", "master", "", None])
    def test_writes_without_a_feature_branch_are_denied(self, branch: str | None) -> None:
        args = {
            "owner": "zulqarnain106612-cpu",
            "repo": "Trade-Bot",
            "files": [{"path": "docs/example.md", "content": "x"}],
        }
        if branch is not None:
            args["branch"] = branch
        assert_denied("mcp__github__push_files", args, contains="non-main feature branch")

    def test_boundary_files_cannot_be_changed_through_github_file_tools(self) -> None:
        assert_denied(
            "mcp__github__update_file",
            {
                "repository_full_name": REPO,
                "branch": "security/agent-boundary",
                "path": ".claude/hooks/pre_tool_use.py",
                "content": "bypass",
            },
            contains="Protected observation-boundary",
        )

    @pytest.mark.parametrize(
        "path",
        [
            ".claude",
            ".claude/settings.local.json",
            ".claude/new-hook.py",
            ".github/workflows",
            ".mcp.json",
            ".github/workflows/new-workflow.yml",
            "src/agent_control/new_gate.py",
            "CLAUDE.md",
        ],
    )
    def test_policy_directories_and_launch_configuration_are_protected(self, path: str) -> None:
        assert_denied(
            "mcp__github__push_files",
            {
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
                "branch": "security/test-boundary",
                "files": [{"path": path, "content": "bypass"}],
            },
            contains="Protected observation-boundary",
        )

    def test_boundary_files_cannot_be_changed_inside_bulk_push(self) -> None:
        assert_denied(
            "mcp__github__push_files",
            {
                "owner": "zulqarnain106612-cpu",
                "repo": "Trade-Bot",
                "branch": "security/agent-boundary",
                "files": [{"path": ".claude/settings.json", "content": "{}"}],
            },
            contains="Protected observation-boundary",
        )

    def test_tree_deletions_are_denied(self) -> None:
        assert_denied(
            "mcp__github__create_tree",
            {
                "repository_full_name": REPO,
                "tree_elements": [{"path": ".claude/settings.json", "sha": None}],
            },
            contains="tree deletions are denied",
        )

    def test_force_ref_updates_are_denied(self) -> None:
        assert_denied(
            "mcp__github__update_ref",
            {
                "repository_full_name": REPO,
                "branch_name": "security/agent-boundary",
                "sha": "abc",
                "force": True,
            },
            contains="force-updating",
        )


class TestSettingsWiring:
    def test_pretool_hook_is_registered_for_every_tool(self) -> None:
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        assert any(
            entry.get("matcher") == "*"
            and any("pre_tool_use.py" in hook["command"] for hook in entry["hooks"])
            for entry in settings["hooks"]["PreToolUse"]
        )

    def test_posttool_observation_hook_is_registered_for_all_successes(self) -> None:
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        assert any(
            entry.get("matcher") == "*"
            and any("observation_gate.py" in hook["command"] for hook in entry["hooks"])
            for entry in settings["hooks"]["PostToolUse"]
        )

    def test_failure_observation_hook_is_registered_for_all_failures(self) -> None:
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        assert any(
            entry.get("matcher") == "*"
            and any("observation_failure.py" in hook["command"] for hook in entry["hooks"])
            for entry in settings["hooks"]["PostToolUseFailure"]
        )

    def test_native_capability_tools_are_denied_in_settings_too(self) -> None:
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        denied = set(settings["permissions"]["deny"])
        for tool in (
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "LS",
            "WebFetch",
            "WebSearch",
        ):
            assert tool in denied
