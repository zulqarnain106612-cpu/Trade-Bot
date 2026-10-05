#!/usr/bin/env python3
"""Deterministic gateway for model-visible successful tool observations.

The underlying tool is allowed to run normally. Only the observation sent back
to Claude is rewritten. The gateway is signal-first: failures keep a tiny
diagnostic neighborhood; successful/noisy output keeps only a small bounded
summary. Structured tool outputs retain their shape so built-in validation
continues to accept the replacement.

The gateway also suppresses exact repeats within a session. A repeated
observation is replaced with a short reference to the earlier observation,
which prevents identical tool results from accumulating in model context.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

MAX_LINES = 24
MAX_DIAGNOSTIC_HITS = 8
MAX_DIAGNOSTIC_CONTEXT = 1
MAX_CHARS = 4000
MAX_SEEN = 256
REPEAT_MARKER = "[observation repeated; see earlier identical observation]"
STATE_ENV = "OBSERVATION_GATE_STATE_DIR"
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
TEXT_KEYS = ("stdout", "stderr", "content", "output", "text", "message")


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
        meaningful = [line for line in lines if line.strip() and not NOISE_RE.search(line)]
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


def _state_path(event: dict[str, object]) -> Path | None:
    session_id = event.get("session_id")
    if not session_id:
        return None

    root = os.environ.get(STATE_ENV)
    if root:
        state_dir = Path(root)
    else:
        project = str(event.get("cwd") or "unknown-project")
        project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
        state_dir = Path("/tmp") / "trade-bot-observation-gate" / project_key

    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{session_id}.json"


def _load_seen(event: dict[str, object]) -> set[str]:
    path = _state_path(event)
    if path is None:
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("seen", [])
        return set(values) if isinstance(values, list) else set()
    except (OSError, ValueError):
        return set()


def _save_seen(event: dict[str, object], seen: set[str]) -> None:
    path = _state_path(event)
    if path is None:
        return
    try:
        trimmed = list(seen)[-MAX_SEEN:]
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"seen": trimmed}, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        pass


def _dedupe_text(text: str, event: dict[str, object], field: str) -> str:
    if not text or len(text) < 24:
        return text

    tool_name = str(next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown"))
    digest = hashlib.sha256(
        f"{tool_name}\0{field}\0{text}".encode("utf-8")
    ).hexdigest()
    seen = _load_seen(event)
    if digest in seen:
        return REPEAT_MARKER

    seen.add(digest)
    _save_seen(event, seen)
    return text


def _minimize_value(
    value: object, event: dict[str, object], field: str
) -> object:
    if isinstance(value, str):
        return _dedupe_text(minimize_text(value), event, field)
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                copied = dict(item)
                copied["text"] = _dedupe_text(
                    minimize_text(copied["text"]),
                    event,
                    f"{field}[{index}].text",
                )
                result.append(copied)
            else:
                result.append(item)
        return result
    return value


def transform(event: dict[str, object]) -> dict[str, object]:
    raw = event.get("tool_response")
    if isinstance(raw, dict):
        result = dict(raw)
        for key in TEXT_KEYS:
            if key in result:
                result[key] = _minimize_value(result[key], event, key)
    elif isinstance(raw, list):
        result = _minimize_value(raw, event, "response")
    else:
        result = _dedupe_text(
            minimize_text(compact_text(raw)),
            event,
            "response",
        )

    if isinstance(result, str):
        tool_name = str(next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown"))
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
