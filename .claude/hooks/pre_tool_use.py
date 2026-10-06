#!/usr/bin/env python3
"""Safety-only PreToolUse hook.

This hook is deliberately separate from observation minimization. Tool input
and execution are never rewritten here. Successful model-visible output is
handled by observation_gate.py after the native tool completes.
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
    from common.command_schema import classify
except Exception:  # pragma: no cover - fail-open path
    classify = None  # type: ignore[assignment]


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
        return (
            "Local CI checks must be invoked with the per-command marker "
            f"{run_marker}."
        )

    for pattern in cfg.get("blocked_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return (
                "Direct local test/check execution is disabled. Use "
                f"{run_marker} python3 {wrapper} run <failed-check> instead; "
                "the wrapper permits only checks that were non-green on the "
                "last completed PR run."
            )
    return ""


def _violations(command: str, policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
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

    if event.get("tool_name") != "Bash":
        _emit("allow")

    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command.strip():
        _emit("allow")

    try:
        policy = _load_policy()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        _fail_open(f"policy unavailable: {exc}")
        return

    if _enforcement(policy) == "off":
        _emit("allow")

    try:
        problems = _violations(command, policy)
    except re.error as exc:
        _fail_open(f"invalid policy regex: {exc}")
        return

    if not problems:
        _emit("allow")

    reason = " ".join(dict.fromkeys(problems))
    if _enforcement(policy) == "warn":
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")

    _emit("deny", reason)


if __name__ == "__main__":
    main()
