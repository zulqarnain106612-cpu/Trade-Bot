"""Permanent contract for the universal observation boundary."""

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


def test_config_carries_single_universal_policy():
    config = json.loads(CONFIG.read_text())
    assert config["enabled"] is True
    assert config["max_read_lines"] == 80
    assert config["policy"]["changed_local_source"] == "diff_first"
