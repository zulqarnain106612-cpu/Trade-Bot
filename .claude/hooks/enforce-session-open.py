#!/usr/bin/env python3
# === SESSION-OPEN-GATE v1 ===
# Blocks ALL tool calls that happen before the user sends the first message.
# Claude Code sets transcript_path in the hook input. We count user turns.
# Zero user turns = session just opened = block everything except git status/log.

import json
import os
import re
import sys


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

# Read the session transcript to count user turns
transcript_path = data.get("transcript_path", "")
user_turns = 0

if transcript_path and os.path.isfile(transcript_path):
    try:
        with open(transcript_path) as f:
            transcript = json.load(f)
        # Transcript is a list of message objects
        if isinstance(transcript, list):
            user_turns = sum(
                1 for msg in transcript if isinstance(msg, dict) and msg.get("role") == "human"
            )
    except Exception:
        # Can't read transcript = can't verify = allow (fail open)
        sys.exit(0)
else:
    # No transcript path = can't verify = allow (fail open)
    sys.exit(0)

# If user has sent at least one message, allow everything
if user_turns >= 1:
    sys.exit(0)

# Zero user turns = session just opened
# Allow ONLY: git status, git log, git branch (orientation reads)
if tool == "Bash":
    cmd = inp.get("command", "").strip()
    allowed_patterns = [
        r"^git\s+status(\s+|$)",
        r"^git\s+log\s+--oneline",
        r"^git\s+branch(\s+|$)",
        r"^git\s+rev-parse",
    ]
    for pat in allowed_patterns:
        if re.match(pat, cmd):
            sys.exit(0)
    block(
        "Tool call blocked: no user message received yet this session. "
        "Claude Code opened a session or switched to this branch automatically. "
        "Waiting for explicit user instruction before taking any action. "
        "Send a message to begin."
    )

# Block all non-Bash tools at session open
block(
    f"{tool} blocked: no user message received yet this session. "
    "Waiting for explicit user instruction."
)
