"""
shell_exec.py — Runtime enforcer for COMMAND_EXEC_SCHEMA.

Executes a command declaration dict (validated against COMMAND_EXEC_SCHEMA)
and returns a bounded result. Raw stdout/stderr NEVER exceeds max_lines before
being returned to the caller.

don't Work identically in:
  - Local terminal sessions, always use github cloud action Workflows for review, test, build
  - Cloud containers (CLAUDE_CODE_REMOTE=false, provisioned by session-start.sh)

Environment differences handled automatically:
  - filter_mode=jq: graceful error if jq binary absent (common on cloud containers)
  - Timeout: process group killed on SIGKILL after timeout (works in both envs)
  - jsonschema: stdlib-only fallback if package absent (no hard import failure)

Usage:
    from common.shell_exec import run

    result = run({
        "command": "pytest tests/ -q",
        "output_policy": {
            "max_lines": 40,
            "stream": "both",
            "filter_mode": "tail",
        },
        "retry_policy": {
            "max_attempts": 2,
            "retry_on": ["nonzero_exit"],
            "delay_s": 1,
        },
    })

    # result["filtered_output"]  — capped, filtered text (only this enters context)
    # result["exit_code"]        — final attempt exit code
    # result["truncated"]        — True if lines were dropped by cap
    # result["attempt_count"]    — how many runs were made
    # result["error"]            — None on success, str on unrecoverable failure
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any

from common.command_schema import COMMAND_EXEC_SCHEMA

# ---------------------------------------------------------------------------
# Validation — jsonschema preferred, lightweight fallback if absent
# ---------------------------------------------------------------------------

try:
    from jsonschema import ValidationError as _JSValidationError
    from jsonschema import validate as _js_validate

    def _validate(declaration: dict) -> None:
        try:
            _js_validate(instance=declaration, schema=COMMAND_EXEC_SCHEMA)
        except _JSValidationError as exc:
            raise ValueError(f"Invalid command declaration: {exc.message}") from exc

except ImportError:
    # jsonschema not installed (e.g. fresh cloud container before pip install).
    # Perform minimal structural checks so the module still works.
    def _validate(declaration: dict) -> None:  # type: ignore[misc]
        if not isinstance(declaration, dict):
            raise ValueError("declaration must be a dict")
        if "command" not in declaration or not declaration["command"]:
            raise ValueError("declaration['command'] is required and must be non-empty")
        if "output_policy" not in declaration:
            raise ValueError("declaration['output_policy'] is required")
        policy = declaration["output_policy"]
        max_lines = policy.get("max_lines")
        if max_lines is None:
            raise ValueError("output_policy['max_lines'] is required")
        if not isinstance(max_lines, int) or not (1 <= max_lines <= 200):
            raise ValueError("output_policy['max_lines'] must be integer 1–200")


# ---------------------------------------------------------------------------
# Internal: capture
# ---------------------------------------------------------------------------


def _capture(command: str, stream: str, timeout: int) -> tuple[int, str]:
    """
    Run command in a new process group so the entire tree can be killed on
    timeout. Works on both local and cloud container environments.
    """
    # bandit B602: shell=True is this module's contract, not an oversight. A
    # declaration's "command" is a shell command *line* -- pipes, globs and
    # redirection are the point (see COMMAND_EXEC_SCHEMA and the jq filter
    # mode), so there is no argv list to pass instead. The string comes from a
    # schema-validated declaration written by a developer, never from network
    # or user input, and this module is developer tooling rather than a request
    # path. Scoped to this call so B602 stays enforced everywhere else.
    proc = subprocess.Popen(  # nosec B602
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # New session (and so a new process group): lets us kill children on
        # timeout. start_new_session, not preexec_fn=os.setsid: preexec_fn
        # runs Python between fork and exec, which is not async-signal-safe
        # and can deadlock if any other thread holds a lock at fork time --
        # and this module is called from FastAPI handlers and asyncio
        # executors, both of which are threaded. start_new_session is the
        # same setsid() call made safely in C, and it is not deprecated.
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire process group, not just the shell. The group is
        # already gone if the command exited between the timeout and this call.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
        raise

    if stream == "stdout":
        text = stdout
    elif stream == "stderr":
        text = stderr
    else:  # both
        text = stdout + stderr

    return proc.returncode, text


# ---------------------------------------------------------------------------
# Internal: filter
# ---------------------------------------------------------------------------


def _filter(text: str, mode: str, expr: str) -> str:
    """Apply pre-cap filter. Returns filtered text (may still exceed max_lines)."""
    if not mode or mode == "none":
        return text

    lines = text.splitlines()

    if mode == "head":
        n = int(expr) if expr and expr.isdigit() else 50
        return "\n".join(lines[:n])

    if mode == "tail":
        n = int(expr) if expr and expr.isdigit() else 50
        return "\n".join(lines[-n:])

    if mode == "grep":
        return "\n".join(line for line in lines if expr in line)

    if mode == "regex":
        try:
            pat = re.compile(expr)
        except re.error as exc:
            raise ValueError(f"filter_expr is not a valid regex: {exc}") from exc
        return "\n".join(line for line in lines if pat.search(line))

    if mode == "jq":
        if not shutil.which("jq"):
            raise RuntimeError(
                "filter_mode=jq requires 'jq' on PATH. "
                "On cloud containers use filter_mode=fields or filter_mode=regex instead. "
                "Install locally: apt-get install jq / brew install jq"
            )
        # check=False: a failed jq query is reported as a ValueError naming
        # the query, which is more useful than CalledProcessError.
        proc = subprocess.run(
            ["jq", "-r", expr],
            input=text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"jq query failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    if mode == "fields":
        # JSON-per-line: extract only listed keys. Non-JSON lines pass through.
        if not expr:
            return text
        keys = [k.strip() for k in expr.split(",") if k.strip()]
        out: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(json.dumps({k: obj[k] for k in keys if k in obj}))
            except (json.JSONDecodeError, TypeError, AttributeError):
                out.append(line)
        return "\n".join(out)

    # Unknown mode — passthrough (schema enum prevents this in practice)
    return text


# ---------------------------------------------------------------------------
# Internal: cap
# ---------------------------------------------------------------------------


def _cap(text: str, max_lines: int) -> tuple[str, bool]:
    """Hard-cap to max_lines. Returns (capped_text, truncated_flag)."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    dropped = len(lines) - max_lines
    kept = lines[:max_lines]
    kept.append(f"... [{dropped} lines dropped — tighten filter_expr, do not raise max_lines]")
    return "\n".join(kept), True


# ---------------------------------------------------------------------------
# Internal: retry check
# ---------------------------------------------------------------------------


def _should_retry(
    exit_code: int,
    output: str,
    retry_on: list[str],
    pattern_absent: str,
) -> bool:
    if "nonzero_exit" in retry_on and exit_code != 0:
        return True
    if "empty_output" in retry_on and not output.strip():
        return True
    return bool(
        "pattern_absent" in retry_on and pattern_absent and not re.search(pattern_absent, output)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(declaration: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """
    Validate declaration, execute command, apply output_policy, honour retry_policy.

    Args:
        declaration: dict conforming to COMMAND_EXEC_SCHEMA.
        timeout:     Per-attempt timeout in seconds. Default 120.
                     Increase for slow commands (long test suites, builds).

    Returns:
        {
            "exit_code":       int,
            "filtered_output": str,   # only this should enter context
            "truncated":       bool,  # True if lines were dropped by cap
            "attempt_count":   int,
            "error":           str | None,
        }

    Raises:
        ValueError:  declaration fails schema validation or filter_expr is bad.
        RuntimeError: filter_mode=jq but jq not on PATH.
    """
    _validate(declaration)

    cmd: str = declaration["command"]
    policy: dict = declaration["output_policy"]
    max_lines: int = policy.get("max_lines", 50)
    stream: str = policy.get("stream", "stdout")
    filter_mode: str = policy.get("filter_mode", "none")
    filter_expr: str = policy.get("filter_expr", "")
    on_empty: str = policy.get("on_empty", "ok")

    retry: dict = declaration.get("retry_policy", {})
    max_attempts: int = retry.get("max_attempts", 1)
    retry_on: list[str] = retry.get("retry_on", [])
    pattern_absent: str = retry.get("pattern_absent", "")
    delay_s: float = retry.get("delay_s", 1.0)

    exit_code: int = -1
    capped: str = ""
    truncated: bool = False
    attempt: int = 0
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            exit_code, raw = _capture(cmd, stream, timeout)
        except subprocess.TimeoutExpired:
            last_error = f"Command timed out after {timeout}s (attempt {attempt})"
            if attempt < max_attempts:
                time.sleep(delay_s)
                continue
            break

        filtered = _filter(raw, filter_mode, filter_expr)
        capped, truncated = _cap(filtered, max_lines)

        # on_empty guard
        if on_empty == "error" and not capped.strip():
            last_error = "empty output after filter"
            effective_retry_on = list(set(retry_on) | {"empty_output"})
            if attempt < max_attempts and _should_retry(
                exit_code, capped, effective_retry_on, pattern_absent
            ):
                time.sleep(delay_s)
                continue
            break

        if (
            attempt < max_attempts
            and retry_on
            and _should_retry(exit_code, capped, retry_on, pattern_absent)
        ):
            time.sleep(delay_s)
            continue

        last_error = None
        break

    return {
        "exit_code": exit_code,
        "filtered_output": capped,
        "truncated": truncated,
        "attempt_count": attempt,
        "error": last_error,
    }
