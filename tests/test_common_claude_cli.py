"""Tests for common/claude_cli.py -- the shared `claude -p` subprocess wrapper."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from common.claude_cli import ClaudeCLIError, _require_cli, run_claude


def test_require_cli_raises_when_not_found():
    with patch("common.claude_cli.shutil.which", return_value=None):
        with pytest.raises(ClaudeCLIError, match="Claude Code CLI not found"):
            _require_cli()


def test_require_cli_passes_when_found():
    with patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"):
        _require_cli()  # must not raise


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_claude_dict_payload():
    payload = {
        "result": "hello",
        "structured_output": None,
        "total_cost_usd": 0.01,
        "session_id": "abc",
    }
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("common.claude_cli.subprocess.run", return_value=_completed(json.dumps(payload))),
    ):
        out = run_claude("prompt", model="haiku")
    assert out == {
        "result": "hello",
        "structured_output": None,
        "cost_usd": 0.01,
        "session_id": "abc",
    }


def test_run_claude_list_payload_unwraps_last_element():
    payload = [
        {"type": "system", "subtype": "init"},
        {
            "result": "final answer",
            "structured_output": {"a": 1},
            "total_cost_usd": 0.02,
            "session_id": "s1",
        },
    ]
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("common.claude_cli.subprocess.run", return_value=_completed(json.dumps(payload))),
    ):
        out = run_claude("prompt", model="haiku", json_schema={"type": "object"})
    assert out["result"] == "final answer"
    assert out["structured_output"] == {"a": 1}
    assert out["cost_usd"] == 0.02


def test_run_claude_empty_list_payload_unwraps_to_empty_dict():
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("common.claude_cli.subprocess.run", return_value=_completed(json.dumps([]))),
    ):
        out = run_claude("prompt", model="haiku")
    assert out == {"result": "", "structured_output": None, "cost_usd": 0.0, "session_id": None}


def test_run_claude_nonzero_exit_raises():
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "common.claude_cli.subprocess.run",
            return_value=_completed("", returncode=1, stderr="boom"),
        ),
        pytest.raises(ClaudeCLIError, match="exit 1"),
    ):
        run_claude("prompt", model="haiku")


def test_run_claude_invalid_json_raises():
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("common.claude_cli.subprocess.run", return_value=_completed("not json")),
    ):
        with pytest.raises(ClaudeCLIError, match="Could not parse"):
            run_claude("prompt", model="haiku")


def test_run_claude_builds_command_with_allowed_tools_and_max_turns():
    payload = {"result": "ok", "structured_output": None, "total_cost_usd": 0.0, "session_id": None}
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "common.claude_cli.subprocess.run", return_value=_completed(json.dumps(payload))
        ) as mock_run,
    ):
        run_claude("prompt", model="sonnet", allowed_tools="Read,Grep", max_turns=3)
    cmd = mock_run.call_args[0][0]
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"
    assert "--max-turns" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "3"
    assert "--json-schema" not in cmd


def test_run_claude_json_schema_omits_max_turns_cap():
    payload = {"result": "ok", "structured_output": {}, "total_cost_usd": 0.0, "session_id": None}
    with (
        patch("common.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "common.claude_cli.subprocess.run", return_value=_completed(json.dumps(payload))
        ) as mock_run,
    ):
        run_claude("prompt", model="haiku", json_schema={"type": "object"}, max_turns=1)
    cmd = mock_run.call_args[0][0]
    assert "--json-schema" in cmd
    assert "--max-turns" not in cmd
