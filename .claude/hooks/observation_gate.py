#!/usr/bin/env python3
"""Universal model-observation boundary for Claude Code tool results.

This hook runs after successful tool execution. Tool execution is unchanged;
only the value returned to the model is transformed. The implementation is
tool-agnostic by default and preserves structured metadata while compacting
large text payloads. A few tool families receive stronger semantic handling.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_DIAGNOSTIC_LINES = 3
MAX_SUMMARY_LINES = 3
MAX_SUMMARY_CHARS = 1200
MIN_COMPACT_CHARS = 1200
MAX_SOURCE_LINES = 30
MAX_CHANGED_DIFF_LINES = 80
MAX_SEARCH_CHARS = 6000
MAX_SEARCH_ITEMS = 40

TOOL_NAME_KEYS = ("tool_name", "toolName")
PRESERVE_TOOLS = {"TaskOutput", "AskUserQuestion", "ExitPlanMode"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}
EXECUTION_TOOLS = {"Bash", "Monitor", "PowerShell"}
SOURCE_TOOLS = {"Read", "NotebookRead"}

SIGNAL_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"warning|warn|exit\s+code|passed|success|completed|changed|created|"
    r"deleted|updated)\b"
)
DIAGNOSTIC_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"exit\s+code|timed?\s*out)\b"
)
SOURCE_SIGNAL_RE = re.compile(
    r"^\s*(?:"
    r"(?:from|import)\s+|(?:async\s+)?def\s+|class\s+"
    r"|(?:if|elif|else|for|while|try|except|finally|with)\b"
    r"|(?:return|raise|yield|assert)\b|@\w+"
    r"|#\s*(?:TODO|FIXME|NOTE|HACK)\b)"
)
NOISE_RE = re.compile(
    r"(?i)^(npm warn|warning:|hint:|notice:|progress|downloading|"
    r"\s*[-\\|/]+\s*$)"
)
LOG_FIELD_NAMES = {
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
    "error",
    "errors",
    "message",
    "detail",
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


def _normalize_lines(text: str) -> list[str]:
    return [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ]


def _finalize(selected: list[str], total: int, *, marker: str) -> str:
    if not selected:
        return "(completed; no diagnostic output)"
    result = "\n".join(selected)
    compacted = total > len(selected)
    if len(result) > MAX_SUMMARY_CHARS:
        result = result[:MAX_SUMMARY_CHARS].rsplit("\n", 1)[0]
        compacted = True
    if compacted:
        result += f"\n{marker}"
    return result


def summarize_execution(text: str) -> str:
    if not text:
        return ""
    lines = _normalize_lines(text)
    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    selected = (diagnostics or [line for line in lines if not NOISE_RE.search(line)])[
        :MAX_DIAGNOSTIC_LINES
    ]
    return _finalize(selected, len(lines), marker="[observation compacted]")


def _file_path(event: dict[str, Any]) -> Path | None:
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve() / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _changed_file_candidates(event: dict[str, Any]) -> str | None:
    """Return only current Git diff candidates for a repeated source read."""
    path = _file_path(event)
    if path is None:
        return None
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    try:
        relative = path.relative_to(project)
    except ValueError:
        return None
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--unified=2", "HEAD", "--", str(relative)],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    diff = proc.stdout.strip()
    if not diff:
        return None
    lines = _normalize_lines(diff)
    if len(lines) <= MAX_CHANGED_DIFF_LINES:
        return _finalize(
            lines,
            len(lines),
            marker="[changed candidates bounded; request a narrower range only if needed]",
        )
    candidates = [
        line
        for line in lines
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@", "+", "-"))
    ][:MAX_CHANGED_DIFF_LINES]
    return _finalize(
        candidates,
        len(lines),
        marker="[changed candidates compacted; request a narrower range only if needed]",
    )


def summarize_source(text: str, event: dict[str, Any]) -> str:
    """Keep bounded reads useful and prefer changed candidates for modified files."""
    candidates = _changed_file_candidates(event)
    if candidates:
        return candidates
    lines = _normalize_lines(text)
    if len(lines) <= MAX_SOURCE_LINES and len(text) < MIN_COMPACT_CHARS:
        return text

    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    if diagnostics:
        selected = diagnostics[:MAX_SOURCE_LINES]
    else:
        selected = lines[:MAX_SOURCE_LINES]
    return _finalize(
        selected,
        len(lines),
        marker="[source observation bounded; request a narrower range for exact content]",
    )


def summarize_generic(text: str) -> str:
    lines = _normalize_lines(text)
    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    if diagnostics:
        selected = diagnostics[:MAX_DIAGNOSTIC_LINES]
    else:
        signals = [line for line in lines if SIGNAL_RE.search(line)]
        meaningful = [line for line in lines if not NOISE_RE.search(line)]
        selected = (signals or meaningful)[:MAX_SUMMARY_LINES]
    return _finalize(selected, len(lines), marker="[observation compacted]")


def summarize_search(text: str, tool: str) -> str:
    lines = _normalize_lines(text)
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) <= MAX_SEARCH_CHARS:
        return joined

    if tool == "Grep":
        path_counts: dict[str, int] = {}
        examples: dict[str, list[str]] = {}
        for line in lines:
            match = re.match(r"(?:[^:]+:)?(?P<path>[^:\n]+):(?P<line>\d+)(?::|$)", line)
            path = match.group("path") if match else line.split(":", 1)[0]
            path_counts[path] = path_counts.get(path, 0) + 1
            examples.setdefault(path, [])
            if len(examples[path]) < 3:
                examples[path].append(line)
        ordered = sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))
        out = [f"[search compacted: {len(lines)} matches across {len(ordered)} paths]"]
        for path, count in ordered[:MAX_SEARCH_ITEMS]:
            out.append(f"{path} ({count} matches)")
            out.extend(f"  {example}" for example in examples[path])
        if len(ordered) > MAX_SEARCH_ITEMS:
            out.append(
                f"[+{len(ordered) - MAX_SEARCH_ITEMS} paths; run a narrower search to inspect them]"
            )
        return "\n".join(out)[:MAX_SEARCH_CHARS]

    paths = sorted(dict.fromkeys(lines))
    directories: dict[str, int] = {}
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        directory = "/".join(parts[:-1]) or "."
        directories[directory] = directories.get(directory, 0) + 1
    out = [f"[search compacted: {len(paths)} entries across {len(directories)} directories]"]
    for directory, count in sorted(directories.items(), key=lambda item: (-item[1], item[0]))[
        :MAX_SEARCH_ITEMS
    ]:
        out.append(f"{directory} ({count} entries)")
    out.append("[Use a narrower search query or a bounded Read for exact content]")
    return "\n".join(out)[:MAX_SEARCH_CHARS]


def _compact_scalar(value: object, *, tool: str, field: str, event: dict[str, Any]) -> object:
    if not isinstance(value, str) or len(value) < MIN_COMPACT_CHARS:
        return value
    if tool in SOURCE_TOOLS:
        return summarize_source(value, event)
    if tool in EXECUTION_TOOLS or field.lower() in LOG_FIELD_NAMES:
        return summarize_execution(value)
    return summarize_generic(value)


def _compact_any(value: object, *, tool: str, field: str = "", event: dict[str, Any] | None = None) -> object:
    if isinstance(value, str):
        return _compact_scalar(value, tool=tool, field=field, event=event or {})
    if isinstance(value, list):
        return [
            _compact_any(item, tool=tool, field=f"{field}[{index}]", event=event)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in {"image", "input_image"}:
            return value
        return {key: _compact_any(item, tool=tool, field=str(key), event=event) for key, item in value.items()}
    return value


def transform(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("tool_response")
    tool = tool_name(event)

    if tool in PRESERVE_TOOLS:
        replacement = raw
    elif tool in SEARCH_TOOLS and isinstance(raw, str):
        replacement = summarize_search(raw, tool)
    elif tool in EXECUTION_TOOLS and isinstance(raw, dict):
        replacement = dict(raw)
        for key in ("stdout", "stderr"):
            if key in replacement:
                replacement[key] = summarize_execution(compact_text(replacement[key]))
    elif tool in EXECUTION_TOOLS and isinstance(raw, str):
        replacement = summarize_execution(raw)
    else:
        replacement = _compact_any(raw, tool=tool, event=event)

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
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                    },
                    "error": f"observation boundary failed: {type(exc).__name__}: {exc}",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
