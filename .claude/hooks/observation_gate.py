#!/usr/bin/env python3
"""Deterministic gateway for model-visible successful tool observations.

The underlying tool is allowed to run normally. Only the observation sent back
to Claude is rewritten. The gateway is signal-first: failures keep a tiny
diagnostic neighborhood; successful/noisy output keeps only a small bounded
summary. Structured tool outputs retain their shape so built-in validation
continues to accept the replacement.
"""

import json
import re
import sys

MAX_LINES = 24
MAX_DIAGNOSTIC_HITS = 8
MAX_DIAGNOSTIC_CONTEXT = 1
MAX_CHARS = 4000
ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed)\b"
)
SIGNAL_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"warning|warn|exit\s+code|passed|success|completed)\b"
)
NOISE_RE = re.compile(
    r"(?i)^(npm warn|warning:|hint:|notice:|progress|downloading|"
    r"\s*[-\\|/]+\s*$)"
)
TOOL_NAME_KEYS = ("tool_name", "toolName")


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


def _diagnostic_lines(lines: list[str]) -> list[str]:
    hits = [i for i, line in enumerate(lines) if ERROR_RE.search(line)]
    if not hits:
        return []

    selected: set[int] = set()
    for index in hits[:MAX_DIAGNOSTIC_HITS]:
        selected.update(
            range(
                max(0, index - MAX_DIAGNOSTIC_CONTEXT),
                min(len(lines), index + MAX_DIAGNOSTIC_CONTEXT + 1),
            )
        )
    return [lines[i] for i in sorted(selected) if lines[i].strip()]


def minimize_text(text: str) -> str:
    if not text:
        return ""

    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    diagnostic = _diagnostic_lines(lines)
    if diagnostic:
        out = diagnostic
    else:
        meaningful = [
            line
            for line in lines
            if line.strip() and not NOISE_RE.search(line)
        ]
        signal = [line for line in meaningful if SIGNAL_RE.search(line)]
        out = (signal or meaningful)[:MAX_LINES]

    if not out:
        return "(tool completed; no concise diagnostic output)"

    result = "\n".join(out)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS].rsplit("\n", 1)[0] + "\n[observation compacted]"
    if len(out) >= MAX_LINES:
        result += "\n[observation compacted]"
    return result


def _minimize_value(value: object) -> object:
    if isinstance(value, str):
        return minimize_text(value)
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                copied = dict(item)
                copied["text"] = minimize_text(copied["text"])
                result.append(copied)
            else:
                result.append(item)
        return result
    return value


def transform(event: dict[str, object]) -> dict[str, object]:
    raw = event.get("tool_response")
    if isinstance(raw, dict):
        result = dict(raw)
        for key in ("stdout", "stderr", "content", "output", "text", "message"):
            if key in result:
                result[key] = _minimize_value(result[key])
    elif isinstance(raw, list):
        result = _minimize_value(raw)
    else:
        result = minimize_text(compact_text(raw))

    if isinstance(result, str):
        tool_name = str(
            next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown")
        )
        result = f"[{tool_name}]\n{result}"

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": result,
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
                    "error": f"observation gate failed: {exc}",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
