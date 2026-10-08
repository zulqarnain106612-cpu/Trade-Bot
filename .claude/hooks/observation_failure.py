#!/usr/bin/env python3
"""Compact failure context for Claude Code PostToolUseFailure.

Claude Code does not expose a replacement field for PostToolUseFailure.
This hook therefore adds a deterministic compact summary only; it does not
claim to erase or replace the platform's original failure record.
"""

from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PROJECT_DIR))

MAX_LINES = 3
MAX_CHARS = 900
ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|exit\s+code|timed?\s*out)\b"
)


def compact(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def summarize(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    selected = [line for line in lines if ERROR_RE.search(line)][:MAX_LINES]
    if not selected:
        selected = lines[:MAX_LINES]
    result = "\n".join(selected) or "tool failed without a diagnostic message"
    compacted = len(lines) > len(selected)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS].rsplit("\n", 1)[0]
        compacted = True
    if compacted:
        result += "\n[failure observation compacted]"
    return result


def main() -> int:
    try:
        event = json.load(sys.stdin)
        raw = event.get("error") or event.get("message")
        summary = summarize(compact(raw))
        try:
            from src.agent_control.reliability import record_tool_failure
            tool = str(event.get("tool_name") or event.get("toolName") or "unknown")
            inp = event.get("tool_input") or {}
            command = inp.get("command", "") if isinstance(inp, dict) else ""
            record_tool_failure(tool=tool, command=str(command), summary=summary)
        except Exception:
            pass
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": (
                    "Compact failure diagnostic only; the native failure result "
                    "cannot be replaced at this hook stage:\n" + summary
                ),
            }
        }
        print(json.dumps(payload, ensure_ascii=False))
    except Exception:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUseFailure",
                        "additionalContext": ("Tool failed; no concise diagnostic was available."),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
