#!/usr/bin/env python3
"""Keep failed-tool feedback concise.

Claude Code does not expose a replacement field for PostToolUseFailure, so this
hook cannot delete the platform's original failure record. It therefore adds
only a deterministic, tiny diagnostic context and never echoes the raw error.
Bash calls are wrapped before execution by pre_tool_use.py, so their failures
normally arrive through PostToolUse instead.
"""

from __future__ import annotations

import json
import re
import sys

MAX_LINES = 3
MAX_CHARS = 900
ERROR_RE = re.compile(r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|exit\s+code)\b")


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
    hits = [line for line in lines if ERROR_RE.search(line)]
    out = (hits or lines)[:MAX_LINES]
    result = "\n".join(out)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS].rsplit("\n", 1)[0] + "\n[failure compacted]"
    return result or "tool failed without a diagnostic message"


def main() -> int:
    try:
        event = json.load(sys.stdin)
        raw = event.get("error") or event.get("tool_response") or event.get("message")
        summary = summarize(compact(raw))
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": f"Failure summary:\n{summary}",
            }
        }, ensure_ascii=False))
    except Exception:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": "Tool failed; no concise diagnostic was available.",
            }
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
