#!/usr/bin/env python3
"""Universal PreToolUse safety, observation and boundary-self-protection guard."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(
    os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])
).resolve()
CONFIG_PATH = PROJECT_DIR / "config" / "observation_boundary.json"
POLICY_PATH = PROJECT_DIR / "config" / "command_policy.json"

HARD_MAX_READ_LINES = 80
DEFAULT_READ_TOOLS = {"Read"}
DEFAULT_MCP_READ_PATTERNS = [r"mcp__Desktop_Commander__read_file$"]

PROTECTED_BOUNDARY_PATHS = frozenset(
    {
        ".claude/settings.json",
        ".claude/hooks/pre_tool_use.py",
        ".claude/hooks/observation_gate.py",
        ".claude/hooks/observation_failure.py",
        "config/observation_boundary.json",
        "config/command_policy.json",
        "src/agent_control/reliability.py",
        "src/agent_control/completion.py",
        "tests/test_observation_boundary_contract.py",
    }
)
PROTECTED_MUTATION_TOOLS = {
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__GitHub__create_file",
    "mcp__GitHub__update_file",
    "mcp__GitHub__delete_file",
}

BASH_MUTATOR_RE = re.compile(
    r"(?i)(?:>|>>|tee\b|sed\s+-i\b|perl\s+-pi\b|"
    r"python(?:3)?\s+(?:-c\b|\S+)|ruby\s+-e\b|"
    r"\b(?:cp|mv|rm|dd|git\s+(?:apply|checkout|restore|reset|clean))\b)"
)

sys.path.insert(0, str(PROJECT_DIR))

try:
    from common.command_schema import CI_LOG_REFUSAL, classify, is_ci_log_access
except Exception:  # pragma: no cover
    CI_LOG_REFUSAL = (
        "CI run data is not an allowed model-observation channel; use the PR notice comment."
    )
    classify = None  # type: ignore[assignment]

    def is_ci_log_access(command: str) -> bool:
        return False


def _emit(decision: str, reason: str = "") -> None:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    raise SystemExit(0)


def _fail_open(note: str) -> None:
    print(f"[pre_tool_use] degraded, allowing: {note}", file=sys.stderr)
    _emit("allow")


def _load_policy() -> dict[str, Any]:
    try:
        with POLICY_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        print(
            "[pre_tool_use] policy unavailable; hard observation controls remain active",
            file=sys.stderr,
        )
        return {}


def _load_observation_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {}


def _enforcement(policy: dict[str, Any]) -> str:
    override = os.environ.get("TB_COMMAND_POLICY", "").strip().lower()
    if override in {"block", "warn"}:
        return override
    configured = str(policy.get("enforcement", "block")).lower()
    return configured if configured in {"block", "warn"} else "block"


def _local_check_violation(command: str, policy: dict[str, Any]) -> str:
    cfg = policy.get("local_checks", {})
    if not cfg.get("enabled", False):
        return ""
    wrapper = str(cfg.get("wrapper", "scripts/local_checks.py"))
    run_marker = str(cfg.get("run_marker", "TB_LOCAL_CHECKS=1"))
    if wrapper in command:
        if re.search(r"\bprepare\b", command) or run_marker in command:
            return ""
        return f"Local CI checks must be invoked with the per-command marker {run_marker}."
    for pattern in cfg.get("blocked_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return (
                "Direct local test/check execution is disabled. Use "
                f"{run_marker} python3 {wrapper} run <failed-check> instead; "
                "the wrapper permits only checks that were non-green on the last completed PR run."
            )
    return ""


def _read_limit(tool_input: dict[str, Any]) -> int | None:
    for key in ("limit", "max_lines", "line_limit", "length"):
        value = tool_input.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    start = tool_input.get("start_line")
    end = tool_input.get("end_line")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return end - start + 1
    return None


def _read_boundary_violation(event: dict[str, Any], _policy: dict[str, Any]) -> str:
    cfg = _load_observation_config()
    tool = str(event.get("tool_name", ""))
    names = set(DEFAULT_READ_TOOLS)
    configured = cfg.get("native_read_tools")
    if isinstance(configured, list):
        names.update(str(item) for item in configured)

    patterns = list(DEFAULT_MCP_READ_PATTERNS)
    configured_patterns = cfg.get("mcp_read_tool_patterns")
    if isinstance(configured_patterns, list):
        patterns.extend(str(item) for item in configured_patterns)

    if tool not in names and not any(
        re.search(pattern, tool, re.IGNORECASE) for pattern in patterns
    ):
        return ""

    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "Read operations require an explicit bounded range."

    if tool == "NotebookRead":
        return ""

    limit = _read_limit(tool_input)
    configured_max = cfg.get("max_read_lines", HARD_MAX_READ_LINES)
    try:
        max_lines = min(int(configured_max), HARD_MAX_READ_LINES)
    except (TypeError, ValueError):
        max_lines = HARD_MAX_READ_LINES

    if limit is None:
        return (
            f"Read operations require an explicit bounded range of at most {max_lines} lines. "
            "Use a targeted search first, then request the smallest exact range needed."
        )
    if limit < 1 or limit > max_lines:
        return f"Read operations are permanently bounded to at most {max_lines} lines."
    return ""


def _protected_boundary_violation(event: dict[str, Any]) -> str:
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""

    targets: list[str] = []
    for key in ("file_path", "path", "filename", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            raw = value.replace("\\", "/")
            marker = str(PROJECT_DIR).replace("\\", "/").rstrip("/") + "/"
            if raw.startswith(marker):
                raw = raw[len(marker) :]
            if raw.startswith("./"):
                raw = raw[2:]
            targets.append(raw.lstrip("/"))

    protected = [path for path in targets if path in PROTECTED_BOUNDARY_PATHS]

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for protected_path in PROTECTED_BOUNDARY_PATHS:
            absolute_path = (PROJECT_DIR / protected_path).as_posix()
            if (protected_path in command or absolute_path in command) and BASH_MUTATOR_RE.search(
                command
            ):
                return f"Protected observation-boundary file mutation denied: {protected_path}."

    if not protected:
        return ""

    if tool in PROTECTED_MUTATION_TOOLS:
        return f"Protected observation-boundary file mutation denied: {protected[0]}."

    if (
        any(key in tool_input for key in ("content", "new_string", "replacement"))
        and tool != "Read"
    ):
        return f"Protected observation-boundary file mutation denied: {protected[0]}."

    return ""


def _violations(event: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    protected_violation = _protected_boundary_violation(event)
    if protected_violation:
        problems.append(protected_violation)

    read_violation = _read_boundary_violation(event, policy)
    if read_violation:
        problems.append(read_violation)

    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    if tool == "Bash" and isinstance(command, str) and is_ci_log_access(command):
        problems.append(CI_LOG_REFUSAL)


    if not isinstance(command, str) or not command.strip():
        return problems

    local_check = _local_check_violation(command, policy)
    if local_check:
        problems.append(local_check)

    secret_cfg = policy.get("secret_echo", {})
    if secret_cfg.get("enabled", True):
        for pattern in secret_cfg.get("patterns", []):
            if re.search(pattern, command, re.IGNORECASE):
                problems.append(
                    "This command would print credentials into the transcript. "
                    "Read the value without echoing it, or check only for presence."
                )
                break

    destructive_cfg = policy.get("destructive", {})
    if destructive_cfg.get("enabled", True) and classify is not None:
        marker = destructive_cfg.get("allow_marker", "TB_DESTRUCTIVE_OK=1")
        if classify(command) == "destructive" and marker not in command:
            problems.append(
                "Destructive command. Use the project's authorized destructive "
                "execution path, or prefix it with "
                f"{marker} after explicit human approval."
            )

    if not problems:
        try:
            from src.agent_control.hooks import git_guard

            guard = git_guard(event) if tool == "Bash" and "git" in command else ""
            if guard:
                problems.append(guard)
        except Exception as exc:  # pragma: no cover
            print(f"[pre_tool_use] git guard degraded, allowing: {exc!r}", file=sys.stderr)

    return problems


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _fail_open("empty hook payload")
            return
        event = json.loads(raw)
    except Exception as exc:
        _fail_open(f"malformed hook payload: {exc}")
        return

    try:
        policy = _load_policy()
        problems = _violations(event, policy)
    except (re.error, TypeError, ValueError) as exc:
        _fail_open(f"invalid policy/configuration: {exc}")
        return

    if not problems:
        _emit("allow")

    reason = " ".join(dict.fromkeys(problems))
    hard = any(
        marker in reason
        for marker in ("Protected observation-boundary", "Read operations", CI_LOG_REFUSAL)
    )
    if _enforcement(policy) == "warn" and not hard:
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")

    _emit("deny", reason)


if __name__ == "__main__":
    main()
