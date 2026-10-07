#!/usr/bin/env python3
"""
Agent task control -- durable task state, Git safety and the completion gate.

    python3 scripts/agent_control.py task create --objective "..."
    python3 scripts/agent_control.py verify-completion

Everything lives in src/agent_control (see src/agent_control/cli.py for the
command table); the protocol is documented in
docs/agent/AGENT_EXECUTION_PROTOCOL.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent_control.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
