import base64
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / ".claude" / "hooks" / "pre_tool_use.py"


def test_bash_input_is_routed_through_capture_boundary():
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "printf 'one\\ntwo\\n'"}}
    )
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    output = json.loads(proc.stdout)["hookSpecificOutput"]
    command = output["updatedInput"]["command"]
    encoded = command.rsplit(" ", 1)[-1]
    assert base64.b64decode(encoded).decode() == "printf 'one\\ntwo\\n'"
    assert "bounded_bash.py" in command
