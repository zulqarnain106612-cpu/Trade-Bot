import importlib.util
import json
from pathlib import Path


def load_gate():
    path = Path(__file__).parents[1] / ".claude" / "hooks" / "observation_gate.py"
    spec = importlib.util.spec_from_file_location("observation_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_minimize_text_keeps_diagnostic_and_context():
    gate = load_gate()
    raw = "noise\ncontext\nERROR: build failed\nnext detail\n" + ("noise\n" * 100)
    result = gate.minimize_text(raw)
    assert "context" in result
    assert "ERROR: build failed" in result
    assert "next detail" in result
    assert len(result) < len(raw)


def test_minimize_text_bounds_success_output_tightly():
    gate = load_gate()
    raw = "\n".join(f"line {i}" for i in range(200))
    result = gate.minimize_text(raw)
    assert result.count("\n") <= gate.MAX_LINES
    assert "line 0" in result
    assert len(result) < len(raw) // 4


def test_empty_text_stays_empty():
    gate = load_gate()
    assert gate.minimize_text("") == ""


def test_transform_preserves_structured_tool_shape():
    gate = load_gate()
    event = {
        "tool_name": "Bash",
        "tool_response": {
            "stdout": "ok\nERROR: test failed\nmore",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    result = gate.transform(event)
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert output["stderr"] == ""
    assert output["interrupted"] is False
    assert "ERROR: test failed" in output["stdout"]


def test_transform_compacts_scalar_response():
    gate = load_gate()
    event = {"tool_name": "Read", "tool_response": "line 1\nline 2"}
    result = gate.transform(event)
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert output.startswith("[Read]\n")
    assert "line 1" in output


def test_transform_compacts_common_structured_text_fields():
    gate = load_gate()
    event = {
        "tool_name": "MCP",
        "tool_response": {
            "content": [{"type": "text", "text": "\n".join(f"line {i}" for i in range(100))}],
            "message": "\n".join(f"line {i}" for i in range(100)),
            "id": "keep-me",
        },
    }
    result = gate.transform(event)
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert output["id"] == "keep-me"
    assert len(output["content"][0]["text"]) < 1000
    assert len(output["message"]) < 1000


def test_main_never_raises_on_bad_input(monkeypatch, capsys):
    gate = load_gate()
    monkeypatch.setattr(gate.sys, "stdin", type("S", (), {"read": lambda self: "{"})())
    assert gate.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "error" in payload
