"""Tests for the PostToolUse observation gate.

Decides:
  - GOV-057 — all model-visible tool observations cross the repository boundary
"""

from importlib import util
from pathlib import Path

ROOT = Path(__file__).parents[1]
GATE_PATH = ROOT / ".claude" / "hooks" / "observation_gate.py"


def load_gate():
    spec = util.spec_from_file_location("observation_gate_v2", GATE_PATH)
    module = util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_read_output_is_compacted_to_three_model_visible_lines():
    gate = load_gate()
    raw = "\n".join(f"{i + 1}\tdef important_source_{i}():" for i in range(200))
    event = {"tool_name": "Read", "tool_response": raw}
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output != raw
    assert len(output.splitlines()) <= 31
    assert "source observation bounded" in output


def test_small_read_output_is_preserved():
    gate = load_gate()
    raw = "1\tdef foo():\n2\t    return 1"
    event = {"tool_name": "Read", "tool_response": raw}
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output == raw


def test_bash_output_is_replaced_without_changing_tool_shape():
    gate = load_gate()
    event = {
        "tool_name": "Bash",
        "tool_response": {
            "stdout": "\n".join(["noise"] * 100 + ["ERROR: build failed", "detail"]),
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert set(output) == {"stdout", "stderr", "interrupted", "isImage"}
    assert "ERROR: build failed" in output["stdout"]
    assert len(output["stdout"].splitlines()) <= 4


def test_bash_success_summary_is_small():
    gate = load_gate()
    raw = "\n".join(f"progress {i}" for i in range(500))
    event = {
        "tool_name": "Bash",
        "tool_response": {
            "stdout": raw,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert len(output["stdout"]) < len(raw) // 10


def test_operational_mcp_logs_are_compacted_but_metadata_survives():
    gate = load_gate()
    raw_logs = "\n".join(["noise"] * 100 + ["ERROR: test failed", "detail"])
    event = {
        "tool_name": "mcp__github__workflow_logs",
        "tool_response": {
            "run_id": 123,
            "status": "failure",
            "logs": raw_logs,
            "body": "keep the full API body",
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output["run_id"] == 123
    assert output["status"] == "failure"
    assert output["body"] == "keep the full API body"
    assert "ERROR: test failed" in output["logs"]


def test_large_arbitrary_mcp_text_is_compacted():
    gate = load_gate()
    raw = "\n".join(f"payload line {i}" for i in range(500))
    event = {
        "tool_name": "mcp__example__search",
        "tool_response": {"content": raw, "id": "abc"},
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output["id"] == "abc"
    assert len(output["content"]) < len(raw)
    assert "observation compacted" in output["content"]


def test_nested_content_blocks_are_compacted_without_destroying_shape():
    gate = load_gate()
    raw = "\n".join(f"detail {i}" for i in range(300))
    event = {
        "tool_name": "mcp__example__tool",
        "tool_response": {
            "content": [
                {"type": "text", "text": raw},
                {"type": "image", "source": {"data": "preserve-me"}},
            ]
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert len(output["content"][0]["text"]) < len(raw)
    assert output["content"][1]["source"]["data"] == "preserve-me"


def test_non_operational_mcp_content_is_preserved_when_small():
    gate = load_gate()
    raw = "\n".join(f"result line {i}" for i in range(20))
    event = {
        "tool_name": "mcp__github__get_pull_request",
        "tool_response": {"body": raw, "number": 414},
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output["body"] == raw
    assert output["number"] == 414


def test_search_results_are_semantically_compacted():
    gate = load_gate()
    raw = "\n".join(f"src/pkg_{i % 3}.py:{i}: matched symbol {i}" for i in range(500))
    event = {"tool_name": "Grep", "tool_response": raw}
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert len(output) < len(raw)
    assert "search compacted" in output
    assert "src/pkg_0.py" in output
    assert "matched symbol 0" in output


def test_large_glob_output_is_compacted_by_directory_not_head_tail():
    gate = load_gate()
    raw = "\n".join(f"src/pkg_{i % 10}/module_{i}.py" for i in range(1000))
    event = {"tool_name": "Glob", "tool_response": raw}
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert len(output) < len(raw)
    assert "1000 entries" in output
    assert "src/pkg_0" in output
    assert "src/pkg_9" in output
    assert "Use a narrower search query" in output


def test_repeated_observations_are_deterministic():
    gate = load_gate()
    event = {
        "session_id": "same-session",
        "tool_name": "Bash",
        "tool_response": {
            "stdout": "a long enough successful command result " * 100,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    first = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    second = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert second["stdout"] == first["stdout"]


def test_empty_execution_output_is_valid():
    gate = load_gate()
    event = {
        "tool_name": "Bash",
        "tool_response": {
            "stdout": "",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert output["stdout"] == ""
    assert output["stderr"] == ""
