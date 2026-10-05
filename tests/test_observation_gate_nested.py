import importlib.util
from pathlib import Path


def load_gate():
    path = Path(__file__).parents[1] / ".claude" / "hooks" / "observation_gate.py"
    spec = importlib.util.spec_from_file_location("observation_gate_extra", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nested_structures_are_compacted():
    gate = load_gate()
    event = {
        "tool_name": "mcp__github__run",
        "tool_response": {
            "outer": {
                "output": "\n".join(f"line {i}" for i in range(100)),
                "items": [{"message": "\n".join(f"detail {i}" for i in range(100))}],
            }
        },
    }
    output = gate.transform(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert len(output["outer"]["output"]) <= gate.MAX_CHARS
    assert len(output["outer"]["items"][0]["message"]) <= gate.MAX_CHARS


def test_diagnostic_output_is_three_lines_or_less():
    gate = load_gate()
    raw = "\n".join(["noise"] * 100 + [f"ERROR: failure {i}" for i in range(10)])
    result = gate.minimize_text(raw)
    assert result.count("\n") <= 2
    assert "ERROR: failure 0" in result
