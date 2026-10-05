#!/usr/bin/env python3
"""Model-observation boundary for successful Claude Code tool calls.

This hook never changes the tool input or execution. It only replaces the
successful result that Claude receives. The policy is semantic rather than a
global line/byte truncation:

* Read/search/navigation tools remain intact because their payload is the data
  the agent explicitly asked to inspect.
* execution tools expose outcome/error signals instead of replaying stdout.
* write/edit tools keep their native structured result.
* MCP/external results are compacted only in fields that are explicitly
  log/diagnostic shaped, while metadata and ordinary content remain intact.

No repeat suppression is used: a replacement must remain useful even if the
earlier observation has been compacted away.
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

# These tools are data retrieval tools. Their successful payload is normally
# the information Claude requested, so replacing it with a generic summary
# would break ordinary agent work.
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

# Tool outputs whose stdout/stderr are execution telemetry rather than the
# primary artifact being inspected.
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

# Only these field names are treated as disposable execution/log payloads.
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

    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line) and line.strip()]
    if diagnostics:
        selected = diagnostics[:MAX_DIAGNOSTIC_LINES]
    else:
        meaningful = [
            line for line in lines
            if line.strip() and not NOISE_RE.search(line)
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
        if len(value) < MIN_COMPACT_CHARS:
            return value
        return summarize_execution(value)

    if isinstance(value, list):
        # Preserve content-block/list shape while compacting only text-bearing
        # log entries. Never discard list items or metadata.
        out = []
        for item in value:
            if isinstance(item, dict):
                copied = dict(item)
                for key in ("text", "stdout", "stderr", "message"):
                    if isinstance(copied.get(key), str) and len(copied[key]) >= MIN_COMPACT_CHARS:
                        copied[key] = summarize_execution(copied[key])
                out.append(copied)
            else:
                out.append(item)
        return out

    return value


def _looks_operational(tool: str) -> bool:
    lowered = tool.lower()
    return any(
        marker in lowered
        for marker in ("log", "console", "trace", "workflow", "ci", "exec", "shell", "command")
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

    # Do not mutate canonical retrieval payloads.
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
        # A broken observation hook must never replace a valid tool result with
        # malformed data. Claude Code therefore keeps the original output.
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
