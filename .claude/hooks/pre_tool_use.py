#!/usr/bin/env python3
"""Universal PreToolUse safety and observation guard.

Hard context-boundary rules are enforced independently of the command-policy
rollback switch: native source reads must be bounded and direct CI run data is
not a model-observation channel. Ordinary destructive/secret/local-check rules
retain their existing enforcement levels.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
POLICY_PATH = PROJECT_DIR / "config" / "command_policy.json"

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
    with POLICY_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _enforcement(policy: dict[str, Any]) -> str:
    override = os.environ.get("TB_COMMAND_POLICY", "").strip().lower()
    if override in {"block", "warn", "off"}:
        return override
    configured = str(policy.get("enforcement", "block")).lower()
    return configured if configured in {"block", "warn", "off"} else "block"


def _local_check_violation(command: str, policy: dict[str, Any]) -> str:
    cfg = policy.get("local_checks", {})
    if not cfg.get("enabled", False):
        return ""

    wrapper = str(cfg.get("wrapper", "scripts/local_checks.py"))
    run_marker = str(cfg.get("run_marker", "TB_LOCAL_CHECKS=1"))
    if wrapper in command:
        if re.search(r"\bprepare\b", command):
            return ""
        if run_marker in command:
            return ""
        return f"Local CI checks must be invoked with the per-command marker {run_marker}."

    for pattern in cfg.get("blocked_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return (
                "Direct local test/check execution is disabled. Use "
                f"{run_marker} python3 {wrapper} run <failed-check> instead; "
                "the wrapper permits only checks that were non-green on the "
                "last completed PR run."
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


def _read_boundary_violation(event: dict[str, Any], policy: dict[str, Any]) -> str:
    cfg = policy.get("observation_boundary", {})
    if not cfg.get("enabled", True):
        return ""

    tool = str(event.get("tool_name", ""))
    names = set(cfg.get("native_read_tools", ["Read"]))
    patterns = [re.compile(p, re.IGNORECASE) for p in cfg.get("mcp_read_tool_patterns", [])]
    is_native_read = tool in names
    is_mcp_read = any(rx.search(tool) for rx in patterns)
    if not (is_native_read or is_mcp_read):
        return ""

    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return str(cfg.get("read_refusal_message", "Read calls require an explicit bound."))

    # NotebookRead is cell-oriented rather than line-oriented; successful
    # output still passes through PostToolUse compaction, so do not invent a
    # line limit for notebooks.
    if tool == "NotebookRead":
        return ""

    limit = _read_limit(tool_input)
    max_lines = int(cfg.get("max_read_lines", 30))
    if limit is None:
        return str(cfg.get("read_refusal_message", "Read calls require an explicit bound."))
    if limit < 1 or limit > max_lines:
        return str(cfg.get("read_refusal_message", "Read calls must be bounded."))
    return ""


def _violations(event: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    # These are hard context-boundary controls. They do not become warnings
    # when TB_COMMAND_POLICY is set to off.
    read_violation = _read_boundary_violation(event, policy)
    if read_violation:
        problems.append(read_violation)

    if tool == "Bash" and isinstance(command, str) and is_ci_log_access(command):
        problems.append(CI_LOG_REFUSAL)

    if _enforcement(policy) == "off":
        return problems

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
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        _fail_open(f"policy unavailable: {exc}")
        return

    try:
        problems = _violations(event, policy)
    except (re.error, TypeError, ValueError) as exc:
        _fail_open(f"invalid policy/configuration: {exc}")
        return

    if not problems:
        _emit("allow")

    reason = " ".join(dict.fromkeys(problems))
    if _enforcement(policy) == "warn" and not any(
        msg in reason for msg in (CI_LOG_REFUSAL, "Read calls")
    ):
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")

    _emit("deny", reason)


if __name__ == "__main__":
    main()
