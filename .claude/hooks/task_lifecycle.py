#!/usr/bin/env python3
"""
SessionStart / PostToolUse(Bash) hook for agent-control task lifecycle.

* SessionStart: print the active task's recovery summary (branch, HEAD,
  phase, blockers, dirty files, last checkpoint, next required action),
  rebuilt from disk -- never from the previous session's memory.
* PostToolUse(Bash): a ``git commit`` is verified and recorded against the
  active task.

No active task makes both no-ops. Logic: src/agent_control/hooks.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PROJECT_DIR))

HOOKS = {"session-start", "post-bash"}

try:
    from src.agent_control.hooks import run_hook
except Exception as exc:  # fail open: lifecycle recording is advisory
    print(f"[task_lifecycle] agent-control unavailable: {exc!r}", file=sys.stderr)
    raise SystemExit(0) from None

if len(sys.argv) != 2 or sys.argv[1] not in HOOKS:
    print(f"usage: task_lifecycle.py {{{'|'.join(sorted(HOOKS))}}}", file=sys.stderr)
    raise SystemExit(0)

raise SystemExit(run_hook(sys.argv[1], sys.stdin, sys.stdout, sys.stderr))
