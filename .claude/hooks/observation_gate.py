#!/usr/bin/env python3
"""Minimize successful Claude tool observations before they re-enter model context.

This hook is intentionally deterministic and runs after a tool has executed.
It does not prevent the underlying command/tool from running; it replaces the
model-visible observation with a compact, task-useful representation.
"""

import json
import re
import sys

MAX_LINES = 80
MAX_CHARS = 12000
ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed)\b"
)
NOISE_RE = re.compile(r"(?i)^(npm warn|warning:|hint:|notice:|progress|downloading)")
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


def minimize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]

    # Preserve high-signal diagnostics first. Include a small amount of
    # surrounding context, while dropping repetitive low-value noise.
    hits = [i for i, line in enumerate(lines) if ERROR_RE.search(line)]
    if hits:
        selected: set[int] = set()
        for i in hits[:20]:
            selected.update(range(max(0, i - 1), min(len(lines), i + 2)))
        out = [lines[i] for i in sorted(selected) if lines[i].strip()]
    else:
        out = [line for line in lines if line.strip() and not NOISE_RE.search(line)]
        out = out[:MAX_LINES]

    if not out:
        return "(tool completed; no concise diagnostic output)"

    result = "\n".join(out)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS].rsplit("\n", 1)[0] + "\n[observation compacted]"
    if len(out) >= MAX_LINES:
        result += "\n[observation compacted]"
    return result


def transform(event: dict[str, object]) -> dict[str, object]:
    tool_name = str(next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown"))
    raw = event.get("tool_response")

    # Claude validates built-in tool response shapes. For structured responses
    # that cannot safely be reconstructed generically, retain a compact JSON
    # representation rather than returning an invalid shape.
    if isinstance(raw, dict):
        result = dict(raw)
        for key in ("stdout", "stderr"):
            if key in result:
                result[key] = minimize_text(compact_text(result[key]))
        if "content" in result and not ("stdout" in result or "stderr" in result):
            result["content"] = minimize_text(compact_text(result["content"]))
    elif isinstance(raw, list):
        result = minimize_text(compact_text(raw))
    else:
        result = minimize_text(compact_text(raw))

    # Keep the tool identity available without retaining the raw payload.
    if isinstance(result, str):
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
        return 0
    except Exception as exc:
        # Never break the underlying tool because the observation filter failed.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse"
            },
            "error": f"observation gate failed: {exc}",
        }))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
