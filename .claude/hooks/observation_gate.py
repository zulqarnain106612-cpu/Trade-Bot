#!/usr/bin/env python3
"""Universal, diff-first model-observation boundary for Claude Code."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(
    os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])
).resolve()
CONFIG_PATH = PROJECT_DIR / "config" / "observation_boundary.json"

HARD_DEFAULTS = {
    "max_success_lines": 80,
    "max_success_chars": 6000,
    "max_source_lines": 80,
    "max_source_chars": 6000,
    "max_diff_lines": 120,
    "max_diff_chars": 10000,
    "max_search_chars": 6000,
    "max_search_items": 40,
    "max_list_items": 40,
}
HARD_CEILINGS = dict(HARD_DEFAULTS)

TOOL_NAME_KEYS = ("tool_name", "toolName")
LOCAL_SOURCE_TOOLS = {"Read", "NotebookRead", "mcp__Desktop_Commander__read_file"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}
EXECUTION_TOOLS = {"Bash", "Monitor", "PowerShell"}

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
DIFF_FIELD_NAMES = {"diff", "patch", "patches", "changes", "changed_files", "changed_paths"}
SOURCE_PATH_KEYS = ("file_path", "path", "filename", "notebook_path")

SIGNAL_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"warning|warn|exit\s+code|passed|success|completed|changed|created|"
    r"deleted|updated|modified|added|removed)\b"
)
DIAGNOSTIC_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|fatal|panic|"
    r"assert(?:ion)?|test\s+failed|command\s+failed|build\s+failed|"
    r"exit\s+code|timed?\s*out)\b"
)
SOURCE_SIGNAL_RE = re.compile(
    r"^\s*(?:(?:from|import)\s+|(?:async\s+)?def\s+|class\s+"
    r"|(?:if|elif|else|for|while|try|except|finally|with)\b"
    r"|(?:return|raise|yield|assert)\b|@\w+"
    r"|#\s*(?:TODO|FIXME|NOTE|HACK)\b)"
)
NOISE_RE = re.compile(
    r"(?i)^(npm warn|warning:|hint:|notice:|progress|downloading|\s*[-\\|/]+\s*$)"
)
GIT_STATUS_RE = re.compile(r"^[ MADRCU?!]{1,2}\s+\S")
COMMAND_DIFF_RE = re.compile(r"(?i)\bgit\s+(?:diff|show)\b")
COMMAND_STATUS_RE = re.compile(r"(?i)\bgit\s+status(?:\s|$)")


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


def tool_name(event: dict[str, Any]) -> str:
    return str(next((event.get(k) for k in TOOL_NAME_KEYS if event.get(k)), "unknown"))


def _limits() -> dict[str, int]:
    limits = dict(HARD_DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = data.get("limits", {})
        for key, ceiling in HARD_CEILINGS.items():
            value = cfg.get(key, limits[key])
            if isinstance(value, bool):
                continue
            limits[key] = max(1, min(int(value), ceiling))
    except (OSError, ValueError, TypeError):
        pass
    return limits


def _normalize_lines(text: str) -> list[str]:
    return [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ]


def _finalize(selected: list[str], total: int, *, max_chars: int, marker: str) -> str:
    if not selected:
        return "(completed; no diagnostic output)"
    result = "\n".join(selected)
    compacted = total > len(selected)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit("\n", 1)[0]
        compacted = True
    if compacted:
        result += f"\n{marker}"
    return result


def _looks_like_diff(text: str) -> bool:
    lines = _normalize_lines(text)
    return any(line.startswith("diff --git ") for line in lines) or (
        sum(1 for line in lines if line.startswith("@@")) >= 1
        and sum(1 for line in lines if line.startswith(("+", "-"))) >= 2
    )


def summarize_diff(text: str, *, path: str = "") -> str:
    limits = _limits()
    lines = _normalize_lines(text)
    if not lines:
        return "[no changed-file diff available]"

    changed = [
        line
        for line in lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    ]
    additions = sum(1 for line in changed if line.startswith("+"))
    deletions = sum(1 for line in changed if line.startswith("-"))

    keep: set[int] = set()
    for index, line in enumerate(lines):
        if (
            line.startswith("diff --git ")
            or line.startswith("index ")
            or line.startswith("--- ")
            or line.startswith("+++ ")
            or line.startswith("@@ ")
        ):
            keep.add(index)
        if line.startswith(("+", "-")):
            keep.update(range(max(0, index - 2), min(len(lines), index + 3)))

    selected = [lines[index] for index in sorted(keep)]
    compacted = len(selected) < len(lines)
    if len(selected) > limits["max_diff_lines"]:
        selected = selected[: limits["max_diff_lines"]]
        compacted = True

    result = "\n".join(selected)
    if len(result) > limits["max_diff_chars"]:
        result = result[: limits["max_diff_chars"]].rsplit("\n", 1)[0]
        compacted = True

    header = (
        f"[changed-file observation: {path or '<unknown>'}; +{additions}/-{deletions}; diff-first]"
    )
    if compacted:
        return f"{header}\n{result}\n[diff observation compacted; request a narrower exact range if needed]"
    return f"{header}\n{result}"


def summarize_execution(text: str, *, command: str = "") -> str:
    if not text:
        return ""
    if COMMAND_DIFF_RE.search(command) or _looks_like_diff(text):
        return summarize_diff(text)

    lines = _normalize_lines(text)
    limits = _limits()
    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    selected = diagnostics[:3]
    if not selected:
        selected = [line for line in lines if not NOISE_RE.search(line)][:3]
    return _finalize(
        selected,
        len(lines),
        max_chars=min(limits["max_success_chars"], 1200),
        marker="[observation compacted]",
    )


def summarize_status(text: str) -> str:
    lines = _normalize_lines(text)
    matches = [line for line in lines if GIT_STATUS_RE.search(line)]
    limit = _limits()["max_search_items"]
    selected = matches[:limit]
    if len(matches) > len(selected):
        selected.append(f"[+{len(matches) - len(selected)} status entries compacted]")
    return "\n".join(selected) if selected else "(clean worktree)"


def summarize_source(text: str) -> str:
    limits = _limits()
    lines = _normalize_lines(text)
    if len(lines) <= limits["max_source_lines"] and len(text) <= limits["max_source_chars"]:
        return text

    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    structural = [line for line in lines if SOURCE_SIGNAL_RE.search(line)]
    selected: list[str] = []
    for line in diagnostics[:12] + structural[:20] + lines[:12] + lines[-8:]:
        if line not in selected:
            selected.append(line)
        if len(selected) >= limits["max_source_lines"]:
            break

    return _finalize(
        selected,
        len(lines),
        max_chars=limits["max_source_chars"],
        marker="[source observation compacted; request the smallest exact range needed]",
    )


def summarize_generic(text: str) -> str:
    lines = _normalize_lines(text)
    limits = _limits()
    diagnostics = [line for line in lines if DIAGNOSTIC_RE.search(line)]
    signals = [line for line in lines if SIGNAL_RE.search(line)]
    meaningful = [line for line in lines if not NOISE_RE.search(line)]
    selected = (diagnostics or signals or meaningful)[: limits["max_success_lines"]]
    return _finalize(
        selected,
        len(lines),
        max_chars=limits["max_success_chars"],
        marker="[observation compacted]",
    )


def summarize_search(text: str, tool: str) -> str:
    lines = _normalize_lines(text)
    limits = _limits()
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) <= limits["max_search_chars"]:
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
        for path, count in ordered[: limits["max_search_items"]]:
            out.append(f"{path} ({count} matches)")
            out.extend(f"  {example}" for example in examples[path])
        if len(ordered) > limits["max_search_items"]:
            out.append(
                f"[+{len(ordered) - limits['max_search_items']} paths; run a narrower search to inspect them]"
            )
        return "\n".join(out)[: limits["max_search_chars"]]

    paths = sorted(dict.fromkeys(lines))
    directories: dict[str, int] = {}
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        directory = "/".join(parts[:-1]) or "."
        directories[directory] = directories.get(directory, 0) + 1
    out = [f"[search compacted: {len(paths)} entries across {len(directories)} directories]"]
    for directory, count in sorted(directories.items(), key=lambda item: (-item[1], item[0]))[
        : limits["max_search_items"]
    ]:
        out.append(f"{directory} ({count} entries)")
    out.append("[Use a narrower search query or a bounded Read for exact content]")
    return "\n".join(out)[: limits["max_search_chars"]]


def _project_path(raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    raw = raw_path.strip()
    if raw.startswith("file://"):
        raw = raw[7:]
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return None


def _repo_relative(path: Path) -> str | None:
    try:
        return path.relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return None


def _source_path(event: dict[str, Any]) -> Path | None:
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    for key in SOURCE_PATH_KEYS:
        path = _project_path(tool_input.get(key))
        if path is not None:
            return path
    return None


def _git_diff_for_path(path: Path) -> str:
    relative = _repo_relative(path)
    if relative is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD", "--no-ext-diff", "--unified=8", "--", relative],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode in (0, 1) else ""


def _is_untracked(path: Path) -> bool:
    relative = _repo_relative(path)
    if relative is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all", "--", relative],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.startswith("??") for line in proc.stdout.splitlines())


def _source_observation(event: dict[str, Any], raw: object) -> object:
    if not isinstance(raw, str):
        return _compact_any(raw, tool=tool_name(event))

    path = _source_path(event)
    if path is not None:
        diff = _git_diff_for_path(path)
        if diff.strip():
            return summarize_diff(diff, path=_repo_relative(path) or str(path))
        if _is_untracked(path):
            return (
                f"[new-file observation: {_repo_relative(path) or path}; "
                "no committed baseline; bounded source excerpt]\n" + summarize_source(raw)
            )
    return summarize_source(raw)


def _compact_scalar(value: object, *, tool: str, field: str, command: str = "") -> object:
    if not isinstance(value, str) or len(value) < 1200:
        return value
    lowered = field.lower()
    if lowered in DIFF_FIELD_NAMES or _looks_like_diff(value):
        return summarize_diff(value)
    if tool in EXECUTION_TOOLS or lowered in LOG_FIELD_NAMES:
        return summarize_execution(value, command=command)
    if tool in LOCAL_SOURCE_TOOLS:
        return summarize_source(value)
    return summarize_generic(value)


def _compact_any(value: object, *, tool: str, field: str = "", command: str = "") -> object:
    if isinstance(value, str):
        return _compact_scalar(value, tool=tool, field=field, command=command)
    if isinstance(value, list):
        limit = _limits()["max_list_items"]
        items = [
            _compact_any(item, tool=tool, field=f"{field}[{index}]", command=command)
            for index, item in enumerate(value[:limit])
        ]
        if len(value) > len(items):
            items.append(f"[+{len(value) - len(items)} list items compacted]")
        return items
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in {"image", "input_image"}:
            return value
        return {
            key: _compact_any(item, tool=tool, field=str(key), command=command)
            for key, item in value.items()
        }
    return value


def transform(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("tool_response")
    tool = tool_name(event)
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    if tool in LOCAL_SOURCE_TOOLS:
        replacement = _source_observation(event, raw)
    elif tool in SEARCH_TOOLS and isinstance(raw, str):
        replacement = summarize_search(raw, tool)
    elif tool in EXECUTION_TOOLS and isinstance(raw, dict):
        replacement = dict(raw)
        for key in ("stdout", "stderr"):
            if key in replacement:
                replacement[key] = (
                    summarize_status(compact_text(replacement[key]))
                    if COMMAND_STATUS_RE.search(command)
                    else summarize_execution(compact_text(replacement[key]), command=command)
                )
    elif tool in EXECUTION_TOOLS and isinstance(raw, str):
        replacement = (
            summarize_status(raw)
            if COMMAND_STATUS_RE.search(command)
            else summarize_execution(raw, command=command)
        )
    else:
        replacement = _compact_any(raw, tool=tool, command=command)

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
                        "updatedToolOutput": (
                            f"[observation withheld: boundary error {type(exc).__name__}]"
                        ),
                    }
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
