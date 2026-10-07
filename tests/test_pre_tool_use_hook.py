"""Contract tests for the universal PreToolUse boundary.

Decides:
  - GOV-011 — model-visible source reads are bounded at 80 lines
  - GOV-012 — Live CI monitoring is refused, not rate-limited
  - GOV-019 — CI run data is unreadable from a session under every condition
  - GOV-057 — all model-visible tool observations cross the repository boundary
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


class TestUniversalReadBoundary:
    def test_native_read_requires_an_explicit_bound(self):
        decision = decide("Read", {"file_path": "/repo/src/main.py"})
        assert decision["permissionDecision"] == "deny"
        assert "bounded range" in decision["permissionDecisionReason"]

    def test_native_read_accepts_at_most_eighty_lines(self):
        assert (
            decide("Read", {"file_path": "/repo/src/main.py", "limit": 80})["permissionDecision"]
            == "allow"
        )

    @pytest.mark.parametrize("limit", [81, 1000])
    def test_native_read_rejects_large_limits(self, limit: int):
        decision = decide("Read", {"file_path": "/repo/src/main.py", "limit": limit})
        assert decision["permissionDecision"] == "deny"

    def test_notebook_reads_rely_on_post_tool_compaction(self):
        assert (
            decide("NotebookRead", {"notebook_path": "/repo/notebook.ipynb"})["permissionDecision"]
            == "allow"
        )

    def test_read_file_mcp_tool_uses_the_same_limit(self):
        assert (
            decide(
                "mcp__Desktop_Commander__read_file",
                {"path": "/repo/src/main.py", "length": 80},
            )["permissionDecision"]
            == "allow"
        )
        decision = decide(
            "mcp__Desktop_Commander__read_file",
            {"path": "/repo/src/main.py", "length": 81},
        )
        assert decision["permissionDecision"] == "deny"


class TestCILogBoundary:
    def test_direct_ci_log_read_is_denied_at_pretool(self):
        decision = decide("Bash", {"command": "gh run view 1 --log"})
        assert decision["permissionDecision"] == "deny"
        assert "permanently unreadable" in decision["permissionDecisionReason"]

    def test_ci_check_results_are_denied(self):
        decision = decide("Bash", {"command": "gh pr checks 313"})
        assert decision["permissionDecision"] == "deny"

    def test_ci_notice_comment_channel_remains_allowed(self):
        assert (
            decide(
                "Bash",
                {"command": "gh pr view 313 --json comments --jq '.comments[-1].body'"},
            )["permissionDecision"]
            == "allow"
        )

    def test_policy_rollback_does_not_disable_hard_observation_controls(self):
        decision = decide(
            "Bash",
            {"command": "gh run view 1 --log"},
            {"TB_COMMAND_POLICY": "off"},
        )
        assert decision["permissionDecision"] == "deny"


class TestLocalChecksAndSafety:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest",
            "python3 -m pytest tests/test_x.py",
            "ruff check src/api/main.py",
            "npm run build",
            "npx vitest run tests/example.test.js",
            "python3 .claude/skills/quality-engineering/scripts/qe_gate.py",
            "bash scripts/arch_gate.sh",
        ],
    )
    def test_direct_local_checks_are_denied(self, command: str):
        decision = decide("Bash", {"command": command})
        assert decision["permissionDecision"] == "deny"
        assert "local test/check" in decision["permissionDecisionReason"].lower()

    def test_prepare_is_allowed(self):
        assert (
            decide(
                "Bash",
                {"command": "python3 scripts/local_checks.py prepare"},
            )["permissionDecision"]
            == "allow"
        )

    def test_marked_wrapper_is_allowed(self):
        command = "TB_LOCAL_CHECKS=1 python3 scripts/local_checks.py run tests"
        assert decide("Bash", {"command": command})["permissionDecision"] == "allow"

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf build",
            "git push --force origin main",
            "git reset --hard HEAD~1",
            "terraform destroy",
        ],
    )
    def test_destructive_commands_are_denied(self, command: str):
        decision = decide("Bash", {"command": command})
        assert decision["permissionDecision"] == "deny"
        assert "destructive" in decision["permissionDecisionReason"].lower()

    def test_explicit_marker_allows_authorized_destructive_command(self):
        assert (
            decide("Bash", {"command": "TB_DESTRUCTIVE_OK=1 rm -rf build"})["permissionDecision"]
            == "allow"
        )

    @pytest.mark.parametrize(
        "command",
        ["cat .env", "cat ~/.ssh/id_rsa", "printenv", "gh auth token"],
    )
    def test_secret_disclosure_is_denied(self, command: str):
        decision = decide("Bash", {"command": command})
        assert decision["permissionDecision"] == "deny"
        assert "credential" in decision["permissionDecisionReason"].lower()


class TestSettingsWiring:
    def test_pretool_hook_is_registered_for_every_tool(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PreToolUse"]
        assert any(
            entry.get("matcher") == "*"
            and any("pre_tool_use.py" in h["command"] for h in entry["hooks"])
            for entry in entries
        )

    def test_observation_hook_is_registered_for_all_successful_tools(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PostToolUse"]
        assert any(
            entry.get("matcher") == "*"
            and any("observation_gate.py" in h["command"] for h in entry["hooks"])
            for entry in entries
        )

    def test_failure_observation_hook_is_registered_for_all_failed_tools(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PostToolUseFailure"]
        assert any(
            entry.get("matcher") == "*"
            and any("observation_failure.py" in h["command"] for h in entry["hooks"])
            for entry in entries
        )
