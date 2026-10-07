import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / ".claude" / "hooks" / "observation_failure.py"


def run_hook(error: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"error": error}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        check=False,
    )
    assert proc.returncode == 0
    return json.loads(proc.stdout)["hookSpecificOutput"]


def test_failure_hook_adds_compact_context_only():
    raw = "\n".join(["noise"] * 100 + ["ERROR: build failed", "detail"])
    output = run_hook(raw)
    context = output["additionalContext"]
    assert "ERROR: build failed" in context
    assert "cannot be replaced" in context
    assert context.count("\n") <= 3


def test_failure_hook_uses_top_level_error_field_only():
    output = run_hook("Exit code 1\nSyntaxError: bad input\n" + ("x" * 5000))
    assert "Exit code 1" in output["additionalContext"]
    assert len(output["additionalContext"]) < 1200
