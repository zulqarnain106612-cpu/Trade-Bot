#!/usr/bin/env python3
"""
Stop hook: the agent-control completion gate.

While the worktree's active agent-control task is ACTIVE and incomplete, the
turn may not end; the exact blocking predicates are returned instead. No
active task makes this a no-op. Logic: src/agent_control/hooks.py (stop).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PROJECT_DIR))

try:
    from src.agent_control.hooks import run_hook
except Exception as exc:  # fail open: a broken control layer must not wedge the session
    print(f"[completion_gate] agent-control unavailable, allowing: {exc!r}", file=sys.stderr)
    raise SystemExit(0) from None

raise SystemExit(run_hook("stop", sys.stdin, sys.stdout, sys.stderr))
