import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / ".claude" / "hooks" / "observation_failure.py"


def test_failure_hook_returns_compact_context():
    raw = "\n".join(["noise"] * 100 + ["ERROR: build failed", "detail"])
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"error": raw}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "ERROR: build failed" in context
    assert context.count("\n") <= 3
