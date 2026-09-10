"""
Tests for the PreToolUse policy hook.

The hook is the only thing that makes CLAUDE.md's command rules binding on a
session that never read CLAUDE.md, so its failure modes matter as much as its
successes: a false deny stops legitimate work, a false allow defeats the
control, and a crash must not brick every Bash call in the session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
HOOK = PROJECT_DIR / ".claude" / "hooks" / "pre_tool_use.py"
POLICY = PROJECT_DIR / "config" / "command_policy.json"


def decide(command: str, tool: str = "Bash", env_extra: dict | None = None) -> dict:
    """Run the hook exactly as Claude Code does and return its decision block."""
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(PROJECT_DIR)
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
    assert proc.returncode == 0, f"hook must always exit 0, got {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout)["hookSpecificOutput"]


def load_policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


class TestPolicyFileIntegrity:
    def test_policy_file_is_valid_json(self):
        load_policy()

    def test_every_regex_in_the_policy_compiles(self):
        policy = load_policy()
        bounded = policy["bounded_output"]
        buckets = [
            bounded["bounded_flag_patterns"],
            bounded["oversized_bound_patterns"],
            bounded["oversized_range_patterns"],
            bounded["oversized_byte_patterns"],
            policy["secret_echo"]["patterns"],
        ]
        for bucket in buckets:
            for pattern in bucket:
                re.compile(pattern)

    def test_enforcement_default_is_block(self):
        assert load_policy()["enforcement"] == "block"

    def test_per_fetch_limit_matches_the_project_directive(self):
        assert load_policy()["bounded_output"]["max_declared_lines"] == 5


class TestAllowsCompliantCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "sed -n '1,5p' README.md",
            "sed -n '6,10p' README.md",
            "ls | head -5",
            "grep -m 5 needle file.py",
            "grep -rn -m 5 needle .",
            "git log --oneline -3",
            "git status",
            "python3 -m pytest -q",
            "ruff check .",
            "wc -l a.py b.py",
            "head -c 500 blob.bin",
            # grep -c emits one count line: bounded by construction.
            "env | grep -c MARKER",
            "echo hi",
            "mkdir -p build",
        ],
    )
    def test_allowed(self, command):
        assert decide(command)["permissionDecision"] == "allow"


class TestBlocksUnboundedReads:
    @pytest.mark.parametrize(
        "command",
        [
            "cat README.md",
            "less README.md",
            "find . -name '*.py'",
            "tree src",
            "git log",
            "git diff",
            "journalctl -u svc",
        ],
    )
    def test_denied(self, command):
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "5 lines" in decision["permissionDecisionReason"]


class TestBlocksOversizedBounds:
    @pytest.mark.parametrize(
        "command",
        [
            "head -100 README.md",
            "tail -n 50 app.log",
            "sed -n '1,80p' README.md",
            "grep -m 40 needle file.py",
            "git log -n 30",
            "head -c 500000 blob.bin",
        ],
    )
    def test_denied(self, command):
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "exceeds" in decision["permissionDecisionReason"]

    def test_the_reason_names_the_offending_number(self):
        reason = decide("head -100 README.md")["permissionDecisionReason"]
        assert "100" in reason


class TestBlocksDestructiveCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf build",
            "git push --force origin main",
            "git reset --hard HEAD~1",
            "terraform destroy",
        ],
    )
    def test_denied(self, command):
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "shell_exec" in decision["permissionDecisionReason"]

    def test_explicit_marker_permits_an_authorized_destructive_command(self):
        assert decide("TB_DESTRUCTIVE_OK=1 rm -rf build")["permissionDecision"] == "allow"

    def test_force_with_lease_is_not_treated_as_destructive(self):
        assert decide("git push --force-with-lease origin br")["permissionDecision"] == "allow"


class TestBlocksSecretEcho:
    @pytest.mark.parametrize(
        "command",
        [
            "cat .env",
            "cat ~/.ssh/id_rsa",
            "printenv",
            "gh auth token",
            "aws configure get aws_secret_access_key",
        ],
    )
    def test_denied(self, command):
        decision = decide(command)
        assert decision["permissionDecision"] == "deny"
        assert "credential" in decision["permissionDecisionReason"].lower()

    def test_presence_check_without_the_value_is_allowed(self):
        assert decide('test -n "$MONGODB_URI" && echo set')["permissionDecision"] == "allow"


class TestHeredocsAndWrites:
    """
    A heredoc body is content, not a command line.

    Without this the policy is self-defeating: the file you cannot write is
    the test file that proves the policy works.
    """

    def test_heredoc_body_quoting_a_blocked_pattern_is_allowed(self):
        command = "cat > notes.md <<'EOF'\nDo not run cat .env in a session.\nEOF"
        assert decide(command)["permissionDecision"] == "allow"

    def test_heredoc_body_quoting_a_destructive_command_is_allowed(self):
        command = "cat > runbook.md <<'EOF'\nStep 3 is rm -rf build, ask first.\nEOF"
        assert decide(command)["permissionDecision"] == "allow"

    def test_heredoc_fed_to_a_shell_is_still_inspected(self):
        # The body of an interpreter heredoc really does execute, so the
        # exemption must not extend to it.
        command = "bash <<'EOF'\nrm -rf /important\nEOF"
        assert decide(command)["permissionDecision"] == "deny"

    def test_redirect_to_a_file_is_not_an_unbounded_read(self):
        assert decide("cat a.txt b.txt > merged.txt")["permissionDecision"] == "allow"

    def test_reading_without_a_redirect_is_still_blocked(self):
        assert decide("cat a.txt b.txt")["permissionDecision"] == "deny"

    def test_a_python_heredoc_body_is_not_a_shell_command_line(self):
        # Shell patterns do not apply to Python source; matching them there
        # flags any script whose text mentions a filtered command.
        command = "python3 - <<'PY'\nprint('cat README.md')\nPY"
        assert decide(command)["permissionDecision"] == "allow"

    def test_redirect_in_a_non_final_command_is_still_a_write(self):
        # A ';' or '&&' starts a new command with its own output. Treating the
        # trailing command as the tail of the earlier pipeline made a plain
        # file write look like an unbounded read.
        command = "git show HEAD:file.yml > /tmp/out.yml && echo done"
        assert decide(command)["permissionDecision"] == "allow"

    def test_an_unbounded_read_after_a_write_is_still_blocked(self):
        # The converse: splitting per command must not let a real unbounded
        # read hide behind an earlier redirect.
        command = "echo hi > /tmp/out.txt && " + "c" + "at README.md"
        assert decide(command)["permissionDecision"] == "deny"

    def test_a_pipe_stage_is_not_a_separate_command(self):
        assert decide("ls -la | head -5")["permissionDecision"] == "allow"

    def test_each_command_in_a_sequence_is_checked(self):
        assert decide("git status; git log")["permissionDecision"] == "deny"


class TestEnforcementLevels:
    def test_off_allows_everything(self):
        decision = decide("cat README.md", env_extra={"TB_COMMAND_POLICY": "off"})
        assert decision["permissionDecision"] == "allow"

    def test_warn_allows_but_does_not_silently_pass(self):
        decision = decide("cat README.md", env_extra={"TB_COMMAND_POLICY": "warn"})
        assert decision["permissionDecision"] == "allow"

    def test_block_is_used_for_an_unrecognised_override(self):
        decision = decide("cat README.md", env_extra={"TB_COMMAND_POLICY": "nonsense"})
        assert decision["permissionDecision"] == "deny"


class TestScopeAndFailureModes:
    def test_non_bash_tools_are_untouched(self):
        assert decide("cat README.md", tool="Read")["permissionDecision"] == "allow"

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

    def test_missing_policy_file_fails_open(self, tmp_path):
        # A hook that fails closed on its own misconfiguration blocks all work
        # and reads as a broken environment rather than a policy decision.
        (tmp_path / ".claude" / "hooks").mkdir(parents=True)
        clone = tmp_path / ".claude" / "hooks" / "pre_tool_use.py"
        clone.write_bytes(HOOK.read_bytes())
        proc = subprocess.run(
            [sys.executable, str(clone)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat x"}}),
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)},
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestSharedClassification:
    """The hook and the runtime must never disagree about what is destructive."""

    def test_hook_uses_the_same_classifier_as_shell_exec(self):
        from common.command_schema import classify

        assert classify("rm -rf build") == "destructive"
        assert decide("rm -rf build")["permissionDecision"] == "deny"
        assert classify("ls -la") == "read_only"
        assert decide("ls -la")["permissionDecision"] == "allow"


class TestSettingsWiring:
    def test_hook_is_registered_for_bash(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PreToolUse"]
        assert any(e.get("matcher") == "Bash" for e in entries)

    def test_registered_command_points_at_the_hook_that_exists(self):
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
        commands = [
            h["command"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]
        ]
        assert any("pre_tool_use.py" in c for c in commands)
        assert HOOK.exists()
