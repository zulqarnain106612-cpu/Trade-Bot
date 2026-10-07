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
    """Return only the current file's changed hunks when a source reread repeats work."""
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
        return diff
    # Prefer actual candidates over unchanged context when a generated or
    # unusually large diff would otherwise re-enter the context wholesale.
    candidates = [
        line for line in lines
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@", "+", "-"))
    ][:MAX_CHANGED_DIFF_LINES]
    return _finalize(
        candidates,
        len(lines),
        marker="[changed candidates compacted; request a narrower range only if needed]",
    )


def summarize_source(text: str, event: dict[str, Any]) -> str:
    """Preserve a bounded read, but prefer changed candidates for modified files."""
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
        # Native reads are already capped by PreToolUse. Keep the bounded range
        # rather than shrinking it to three lines and forcing a reread.
        selected = lines[:MAX_SOURCE_LINES]
    return _finalize(
        selected,
        len(lines),
        marker="[source observation bounded; request a narrower range for exact content]",
    )

