#!/usr/bin/env python3
"""Model-observation boundary for successful Claude Code tool calls.

Native tool input and execution are never changed. Only the successful result
returned to the model is replaced.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MAX_DIAGNOSTIC_LINES = 3
MAX_SUMMARY_LINES = 3
MAX_SUMMARY_CHARS = 1200
MIN_COMPACT_CHARS = 1200

TOOL_NAME_KEYS = ("tool_name", "toolName")
PRESERVE_TOOLS = {
    "Read",
    "Grep",
    "Glob",
    "LS",
    "NotebookRead",
    "TaskOutput",
    "AskUserQuestion",
    "ExitPlanMode",
}
EXECUTION_TOOLS = {"Bash", "Monitor", "PowerShell"}
DIAGNOSTIC_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"exit\s+code|timed?\s*out)\b"
)
SIGNAL_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"warning|warn|exit\s+code|passed|success|completed|changed|created|"
    r"deleted|updated)\b"
)
NOISE_RE = re.compile(
    r"(?i)^(npm warn|warning:|hint:|notice:|progress|downloading|"
    r"\s*[-\\|/]+\s*$)"
)
LOG_FIELDS = {
    "stdout",
    "stderr",
    "log",
    "logs",
    "console",
    "trace",
    "stack",
    "stacktrace",
    "diagnostics",
    "raw_log",
    "raw_logs",
    "execution_log",
    "test_output",
    "build_output",
}


def tool_name(event: dict[str, Any]) -> str:
    return str(next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown"))


def compact_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def summarize_execution(text: str) -> str:
    if not text:
        return ""

    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    diagnostics = [
        line for line in lines if line.strip() and DIAGNOSTIC_RE.search(line)
    ]
    if diagnostics:
        selected = diagnostics[:MAX_DIAGNOSTIC_LINES]
    else:
        meaningful = [
            line for line in lines if line.strip() and not NOISE_RE.search(line)
        ]
        signals = [line for line in meaningful if SIGNAL_RE.search(line)]
        selected = (signals or meaningful)[:MAX_SUMMARY_LINES]

    if not selected:
        return "(completed; no diagnostic output)"

    result = "\n".join(selected)
    if len(result) > MAX_SUMMARY_CHARS:
        result = result[:MAX_SUMMARY_CHARS].rsplit("\n", 1)[0] + "\n[observation compacted]"
    elif len(selected) >= MAX_SUMMARY_LINES or len(diagnostics) > MAX_DIAGNOSTIC_LINES:
        result += "\n[observation compacted]"
    return result


def _compact_log_value(value: object) -> object:
    if isinstance(value, str):
        return (
            summarize_execution(value)
            if len(value) >= MIN_COMPACT_CHARS
            else value
        )

    if isinstance(value, list):
        output = []
        for item in value:
            if isinstance(item, dict):
                copied = dict(item)
                for key in ("text", "stdout", "stderr", "message"):
                    text = copied.get(key)
                    if isinstance(text, str) and len(text) >= MIN_COMPACT_CHARS:
                        copied[key] = summarize_execution(text)
                output.append(copied)
            else:
                output.append(item)
        return output

    return value


def _looks_operational(tool: str) -> bool:
    lowered = tool.lower()
    return any(
        marker in lowered
        for marker in (
            "log",
            "console",
            "trace",
            "workflow",
            "ci",
            "exec",
            "shell",
            "command",
        )
    )


def _compact_mcp_result(value: object, tool: str) -> object:
    if not _looks_operational(tool):
        return value

    if isinstance(value, dict):
        result = dict(value)
        for key, item in value.items():
            if str(key).lower() in LOG_FIELDS:
                result[key] = _compact_log_value(item)
        return result

    if isinstance(value, list):
        return _compact_log_value(value)

    if isinstance(value, str) and len(value) >= MIN_COMPACT_CHARS:
        return summarize_execution(value)

    return value


def transform(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("tool_response")
    tool = tool_name(event)

    if tool in PRESERVE_TOOLS:
        replacement = raw
    elif tool in EXECUTION_TOOLS:
        if isinstance(raw, dict):
            replacement = dict(raw)
            for key in ("stdout", "stderr"):
                if key in replacement:
                    replacement[key] = summarize_execution(
                        compact_text(replacement[key])
                    )
        elif isinstance(raw, str):
            replacement = summarize_execution(raw)
        else:
            replacement = raw
    else:
        replacement = _compact_mcp_result(raw, tool)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replacement,
        }
    }


def main() -> int:
    try:
        event = json.load(sys.stdin)
        print(json.dumps(transform(event), ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {"hookEventName": "PostToolUse"},
                    "error": f"observation gate failed: {type(exc).__name__}: {exc}",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
