"""Contract tests for the safety-only PreToolUse hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
HOOK = PROJECT_DIR / ".claude" / "hooks" / "pre_tool_use.py"


def decide(command: str, tool: str = "Bash", env_extra: dict | None = None) -> dict:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
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


class TestExecutionIsNotRestrictedByOutputShape:
    @pytest.mark.parametrize(
        "command",
        [
            "cat README.md",
            "git diff",
            "git log",
            "gh run view 1 --log",
            "gh run view --job 1 --log-failed",
            "gh pr checks 414",
            "tail -f ci.log",
        ],
    )
    def test_output_volume_and_ci_access_are_not_denied(self, command: str):
        assert decide(command)["permissionDecision"] == "allow"

    def test_monitor_is_not_denied_by_observation_policy(self):
        assert decide("", tool="Monitor")["permissionDecision"] == "allow"


class TestLocalChecksAreNarrow:
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
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "local test/check" in decision["permissionDecisionReason"].lower()

    def test_prepare_is_allowed(self):
        assert decide("python3 scripts/local_checks.py prepare")["permissionDecision"] == "allow"

    def test_run_requires_the_explicit_marker(self):
        decision = decide("python3 scripts/local_checks.py run tests")
        assert decision["permissionDecision"] == "deny"

    def test_marked_wrapper_is_allowed(self):
        command = "TB_LOCAL_CHECKS=1 python3 scripts/local_checks.py run tests"
        assert decide(command)["permissionDecision"] == "allow"


class TestSafetyStillApplies:
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
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "destructive" in decision["permissionDecisionReason"].lower()

    def test_explicit_marker_allows_authorized_destructive_command(self):
        assert decide("TB_DESTRUCTIVE_OK=1 rm -rf build")["permissionDecision"] == "allow"

    @pytest.mark.parametrize(
        "command",
        ["cat .env", "cat ~/.ssh/id_rsa", "printenv", "gh auth token"],
    )
    def test_secret_disclosure_is_denied(self, command: str):
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "credential" in decision["permissionDecisionReason"].lower()


class TestFailureModes:
    def test_non_bash_tools_are_untouched(self):
        assert decide("anything", tool="Read")["permissionDecision"] == "allow"

    def test_empty_command_is_allowed(self):
        assert decide("")["permissionDecision"] == "allow"

    def test_malformed_payload_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="{not json",
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(PROJECT_DIR)},
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestSettingsWiring:
    def test_observation_hook_is_registered_for_all_successful_tools(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PostToolUse"]
        assert any(
            entry.get("matcher") == "*"
            and any("observation_gate.py" in h["command"] for h in entry["hooks"])
            for entry in entries
        )

    def test_failure_hook_is_not_registered(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        assert "PostToolUseFailure" not in settings["hooks"]
