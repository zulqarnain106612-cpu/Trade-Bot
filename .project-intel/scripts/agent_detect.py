#!/usr/bin/env python3
"""
Agent Detector
==============
Detects which agent is currently active from environment + git config.
Returns: "claude" | "copilot" | "amazonq" | "human" | "unknown"

Detection priority:
  1. .project-intel/.active_agent file (explicit override)
  2. git config user.name  (set by each agent's commit script)
  3. Environment variables (VSCODE_PID, Q_SET_PARENT_CHECK, etc.)
  4. Default: "unknown"
"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

INTEL_DIR    = Path(__file__).parent.parent
AGENT_FILE   = INTEL_DIR / ".active_agent"


def detect() -> str:
    # 1. Explicit override file (highest priority)
    if AGENT_FILE.exists():
        agent = AGENT_FILE.read_text().strip().lower()
        if agent in ("claude", "copilot", "amazonq", "human"):
            return agent

    # 2. git config user.name
    try:
        name = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=3
        ).stdout.strip().lower()
        if "claude" in name or "anthropic" in name:
            return "claude"
        if "copilot" in name or "github" in name:
            return "copilot"
        if "amazon" in name or "q developer" in name:
            return "amazonq"
    except Exception:
        pass

    # 3. Environment variables
    env = os.environ
    if env.get("Q_SET_PARENT_CHECK") or env.get("AWS_EXECUTION_ENV", "").startswith("Q"):
        return "amazonq"
    if env.get("VSCODE_PID") or env.get("VSCODE_INJECTION"):
        # VSCode = could be Copilot or Amazon Q plugin
        # Disambiguate: check if Amazon Q env is also present
        if env.get("Q_SET_PARENT_CHECK"):
            return "amazonq"
        return "copilot"
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_API_KEY"):
        return "claude"

    return "unknown"


def set_active(agent: str):
    """Write active agent to file so all tools agree."""
    AGENT_FILE.write_text(agent)


def clear():
    if AGENT_FILE.exists():
        AGENT_FILE.unlink()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        set_active(sys.argv[1])
        print(f"Active agent set to: {sys.argv[1]}")
    else:
        print(detect())
