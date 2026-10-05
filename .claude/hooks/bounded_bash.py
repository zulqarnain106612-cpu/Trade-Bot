#!/usr/bin/env python3
"""Execute a Bash tool command while keeping raw stdout/stderr out of Claude context.

The command still runs unchanged. This process captures its complete output locally,
then emits only a compact observation. It intentionally exits zero so Claude Code
uses the normal PostToolUse path; the real exit status is included in the compact
observation.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
GATE = PROJECT_DIR / ".claude" / "hooks" / "observation_gate.py"


def _load_minimizer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("observation_gate", GATE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load observation gate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.minimize_text


def main() -> int:
    if len(sys.argv) != 2:
        print("[bash result]\nexit_code=2\nwrapper: missing encoded command")
        return 0

    try:
        command = base64.b64decode(sys.argv[1], validate=True).decode("utf-8")
    except Exception as exc:
        print(f"[bash result]\nexit_code=2\nwrapper: invalid command encoding: {exc}")
        return 0

    try:
        minimize_text = _load_minimizer()
        with tempfile.TemporaryDirectory(prefix="trade-bot-bash-") as tmp:
            stdout_path = Path(tmp) / "stdout"
            stderr_path = Path(tmp) / "stderr"
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                proc = subprocess.run(
                    ["/bin/bash", "-lc", command],
                    cwd=os.getcwd(),
                    env=os.environ.copy(),
                    stdin=None,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                )

            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            combined = "\n".join(part for part in (stdout_text, stderr_text) if part)
            summary = minimize_text(combined)
            if not summary:
                summary = "(no output)"
            print(f"[bash result]\nexit_code={proc.returncode}\n{summary}")
    except Exception as exc:
        print(f"[bash result]\nexit_code=2\nwrapper failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
