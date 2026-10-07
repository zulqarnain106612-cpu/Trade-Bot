#!/usr/bin/env python3
"""
PreCompact hook: persist the active task's checkpoint before context is lost.

The checkpoint is written and read back; if either fails, compaction is
blocked so the task state is not lost along with the context. No active task
makes this a no-op. Logic: src/agent_control/hooks.py (precompact).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PROJECT_DIR))

try:
    from src.agent_control.hooks import run_hook
except Exception as exc:  # fail open: nothing importable means nothing to persist
    print(f"[precompact_checkpoint] agent-control unavailable, allowing: {exc!r}", file=sys.stderr)
    raise SystemExit(0) from None

raise SystemExit(run_hook("precompact", sys.stdin, sys.stdout, sys.stderr))
