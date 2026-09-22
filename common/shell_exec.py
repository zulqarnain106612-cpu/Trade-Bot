"""
shell_exec.py — Runtime enforcer for COMMAND_EXEC_SCHEMA.

Executes a command declaration dict (validated against COMMAND_EXEC_SCHEMA)
and returns a bounded result. Raw stdout/stderr NEVER exceeds max_lines before
being returned to the caller.

Works identically in:
  - Local terminal sessions
  - Cloud containers (CLAUDE_CODE_REMOTE=true, provisioned by session-start.sh)

That is a statement about this module, not a licence to run heavy work locally:
review, test and build belong in GitHub Actions workflows.

Environment differences handled automatically:
  - filter_mode=jq: graceful error if jq binary absent (common on cloud containers)
  - Timeout: process group killed on SIGKILL after timeout (works in both envs)
  - jsonschema: stdlib-only fallback if package absent (no hard import failure).
    The fallback enforces the same contract, not a reduced one -- see
    _validate_fallback and SEC-0002.

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
    # result["purpose"]          — the declared purpose, echoed back
    # result["exit_code"]        — final attempt exit code
    # result["truncated"]        — True if lines were dropped by cap
    # result["attempt_count"]    — how many runs were made
    # result["error"]            — None on success, str on unrecoverable failure
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from common.command_schema import (
    CI_LOG_REFUSAL,
    COMMAND_EXEC_SCHEMA,
    SCHEMA_VERSION,
    build_env,
    check_schema_version,
    classify,
    is_ci_log_access,
    rank,
    redact,
)

# ---------------------------------------------------------------------------
# Validation — jsonschema preferred, lightweight fallback if absent
# ---------------------------------------------------------------------------

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


def _type_matches(value: Any, expected: str) -> bool:
    """One JSON Schema `type` check, with the two traps Python sets."""
    if expected == "null":
        return value is None
    types = _JSON_TYPES.get(expected)
    if types is None:  # pragma: no cover - schema authors' typo
        raise ValueError(f"unknown type in schema: {expected!r}")
    # bool is a subclass of int, so `True` would satisfy integer/number.
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


def _validate_against(value: Any, schema: dict, path: str) -> None:
    """
    Validate `value` against the draft-07 subset COMMAND_EXEC_SCHEMA uses.

    Walking the schema rather than restating its rules is the point: this
    function has no knowledge of `classification`, `max_bytes` or any other
    field, so adding a constraint to the schema tightens both validation paths
    at once and neither can drift from the other (SEC-0002). It supports
    exactly the keywords the schema uses; an unsupported keyword is a loud
    error, not a silent pass, so extending the schema with something this
    cannot check fails here instead of going unenforced.
    """
    supported = {
        "$schema",
        "$id",
        "title",
        "description",
        "default",
        "readOnly",
        "type",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "maxProperties",
    }
    # `const` and `uniqueItems` are deliberately absent: COMMAND_EXEC_SCHEMA
    # uses neither, and supporting a keyword no schema exercises is untested
    # code pretending to be a control. Adding either to the schema raises the
    # unknown-keyword error above, which is the loud failure that makes the
    # omission safe.
    if unknown := set(schema) - supported:
        raise ValueError(
            f"{path}: fallback validator cannot check schema keyword(s) "
            f"{sorted(unknown)}; extend _validate_against rather than leaving "
            f"the constraint unenforced without jsonschema"
        )

    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, opt) for opt in options):
            raise ValueError(f"{path}: expected type {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path}: more than maxItems {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                _validate_against(item, schema["items"], f"{path}[{i}]")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(f"{path}: missing required property {name!r}")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise ValueError(f"{path}: more than maxProperties {schema['maxProperties']}")

        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_against(item, properties[key], f"{path}.{key}")
            elif extra is False:
                raise ValueError(f"{path}: additional property {key!r} is not allowed")
            elif isinstance(extra, dict):
                _validate_against(item, extra, f"{path}.{key}")


def _validate_fallback(declaration: dict) -> None:
    """
    Validate a declaration without jsonschema, to the same strictness.

    The 1.1.0 fallback checked `command` and `max_lines` and nothing else, so
    on a host with no jsonschema the declared `classification`, the
    `confirm_destructive` type, `additionalProperties: False`, the `timeout_s`
    bounds and every `output_policy` cap went unchecked. A missing dependency
    quietly downgraded the controls this module exists to apply, which is the
    worst shape a security control can fail in (SEC-0002).
    """
    if not isinstance(declaration, dict):
        raise ValueError("declaration must be a dict")
    try:
        _validate_against(declaration, COMMAND_EXEC_SCHEMA, "declaration")
    except ValueError as exc:
        raise ValueError(f"Invalid command declaration: {exc}") from exc


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
    # The fallback enforces the same contract; it is not a reduced one.
    _validate = _validate_fallback  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

AUDIT_LOGGER_NAME = "tradebot.command_audit"

# No handler is attached here: a library that configures logging takes a
# decision that belongs to the host. With no handler the record is a no-op,
# which is why the test asserts on the record rather than on a file.
_audit_log = logging.getLogger(AUDIT_LOGGER_NAME)


def _audited_refusal(
    declaration: dict,
    declared: str,
    detected: str,
    reason: str,
    purpose: str | None,
) -> dict[str, Any]:
    """Build a refusal result and audit it, so no refusal escapes the trail."""
    # .get, not []: the version gate runs before _validate(), so `command` is
    # not yet known to be present when a bad version is refused.
    result = _refused(str(declaration.get("command", "")), declared, reason, purpose)
    _audit(
        outcome="refused",
        declaration=declaration,
        declared=declared,
        detected=detected,
        result=result,
    )
    return result


def _audit_entry(
    *,
    outcome: str,
    declaration: dict,
    declared: str,
    detected: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """The record itself, split out so tests can assert its shape directly."""
    return {
        "schema_version": declaration.get("schema_version", SCHEMA_VERSION),
        "outcome": outcome,
        "command_sha256": result["command_sha256"],
        "purpose": declaration.get("purpose"),
        "classification": declared,
        "detected_classification": detected,
        "exit_code": result["exit_code"],
        "attempt_count": result["attempt_count"],
        "duration_s": result["duration_s"],
        "truncated": result["truncated"],
        "bytes_truncated": result["bytes_truncated"],
        "timed_out": result["timed_out"],
        "redactions_applied": result["redactions_applied"],
        "started_at": result["started_at"],
        "error": result["error"],
    }


def _audit(
    *,
    outcome: str,
    declaration: dict,
    declared: str,
    detected: str,
    result: dict[str, Any],
) -> None:
    """
    Emit one structured record per run() call, refusals included.

    From 1.1.0 the schema told readers that `purpose` was "logged with the
    command hash". No log record existed, so the audit trail a reviewer would
    have relied on to answer "what did the agent run, and why" was not there
    (SEC-0003). A refused command is the record an incident review most wants:
    it says an agent attempted something the contract stopped.

    The command string is deliberately absent. This record goes wherever the
    host sends logs -- a file, an aggregator -- and a command line routinely
    carries a token or a connection string. command_sha256 identifies it
    without republishing it.
    """
    entry = _audit_entry(
        outcome=outcome,
        declaration=declaration,
        declared=declared,
        detected=detected,
        result=result,
    )
    _audit_log.info(json.dumps(entry, sort_keys=True))


# ---------------------------------------------------------------------------
# Internal: capture
# ---------------------------------------------------------------------------


def _command_hash(command: str) -> str:
    """
    Stable identifier for a command string.

    Logged with result and purpose so an audit can correlate "what was
    declared" with "what ran" without storing the command text itself, which
    may embed a path or an argument that is sensitive in aggregate.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]


def _refused(
    command: str,
    classification: str,
    reason: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    """
    Build the result of a declaration rejected before execution.

    Shaped exactly like a normal result (same keys, same types) so callers
    never need a second code path: exit_code=-1 and a non-None error already
    mean "did not succeed". attempt_count=0 is the tell that nothing ran.
    """
    return {
        "exit_code": -1,
        "filtered_output": "",
        "purpose": purpose,
        "truncated": False,
        "bytes_truncated": False,
        "timed_out": False,
        "attempt_count": 0,
        "duration_s": 0.0,
        "classification": classification,
        "redactions_applied": 0,
        "command_sha256": _command_hash(command),
        "started_at": datetime.now(UTC).isoformat(),
        "error": f"refused: {reason}",
    }


def _capture(
    command: str,
    stream: str,
    timeout: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
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
        cwd=cwd,
        # env=None inherits the parent environment (pre-1.1.0 behaviour).
        # A dict from build_env() replaces it wholesale, which is how the
        # env.allowlist secret-containment guarantee is actually enforced.
        env=env,
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


def _cap_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """
    Hard-cap to max_bytes of UTF-8. Returns (capped_text, truncated_flag).

    Applied after the line cap because the line cap is not a size bound: a
    minified bundle, a base64 payload or a no-newline progress log is one
    line and can be megabytes. Truncation is done on the encoded bytes and
    decoded with errors="ignore" so a cut inside a multi-byte codepoint
    drops that codepoint instead of raising.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    note = f"\n... [{len(raw) - max_bytes} bytes dropped — tighten filter_expr]"
    budget = max(0, max_bytes - len(note.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore") + note, True


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
    # ---- version contract (schema 1.2.0) -------------------------------
    # Deliberately ahead of _validate(): when the declared version is one this
    # runtime does not implement, COMMAND_EXEC_SCHEMA is the wrong schema to
    # judge the declaration by, and a validation error about some field would
    # describe the symptom rather than the cause. The schema's `enum` keeps
    # this as defence in depth for callers that validate without run().
    if not isinstance(declaration, dict):
        raise ValueError("declaration must be a dict")
    try:
        check_schema_version(declaration.get("schema_version"))
    except ValueError as exc:
        return _audited_refusal(
            declaration, "read_only", "read_only", str(exc), declaration.get("purpose")
        )

    _validate(declaration)

    cmd: str = declaration["command"]
    policy: dict = declaration["output_policy"]
    purpose: str | None = declaration.get("purpose")

    # ---- effect-class contract (schema 1.1.0) --------------------------
    # A declaration that under-states its effect is a hard error, not a
    # warning: the whole point of declaring is that the declaration, not the
    # shell string, is what a reviewer and the PreToolUse hook read.
    declared: str = declaration.get("classification", "read_only")
    detected: str = classify(cmd)

    # ---- CI observability contract -------------------------------------
    # Ahead of every output control, because this is not an output-size
    # question. No line bound, filter or redaction makes reading a CI log
    # allowed: the pull-request notice is the only channel, and it already
    # carries the failing lines. Refused here as well as in the PreToolUse
    # hook so that routing a command through this module -- the sanctioned way
    # to run anything -- is not a way around the hook.
    if is_ci_log_access(cmd):
        return _refused(cmd, declared, CI_LOG_REFUSAL)
    if rank(detected) > rank(declared):
        return _audited_refusal(
            declaration,
            declared,
            detected,
            f"declaration says classification={declared!r} but the command "
            f"matches a {detected!r} pattern. Correct the declaration -- "
            f"do not weaken the check.",
            purpose,
        )
    if declared == "destructive" and not declaration.get("confirm_destructive", False):
        return _audited_refusal(
            declaration,
            declared,
            detected,
            "destructive command requires confirm_destructive=True and "
            "explicit user authorization.",
            purpose,
        )

    cwd: str | None = declaration.get("cwd")
    if cwd is not None and not os.path.isdir(cwd):
        return _audited_refusal(
            declaration,
            declared,
            detected,
            f"cwd does not exist or is not a directory: {cwd!r}",
            purpose,
        )

    child_env = build_env(declaration.get("env"))
    # Declared timeout wins over the run() kwarg; the kwarg stays the default
    # for pre-1.1.0 declarations that carry no timeout_s.
    timeout = int(declaration.get("timeout_s", timeout))
    max_lines: int = policy.get("max_lines", 50)
    stream: str = policy.get("stream", "stdout")
    filter_mode: str = policy.get("filter_mode", "none")
    filter_expr: str = policy.get("filter_expr", "")
    on_empty: str = policy.get("on_empty", "ok")
    max_bytes: int = policy.get("max_bytes", 65536)
    redact_spec: dict = policy.get("redact", {})
    redact_on: bool = redact_spec.get("enabled", True)
    redact_extra: list[str] = redact_spec.get("extra_patterns", [])

    retry: dict = declaration.get("retry_policy", {})
    max_attempts: int = retry.get("max_attempts", 1)
    if declared == "destructive":
        # CLAUDE.md hard rule: never retry a destructive command. A partially
        # applied rm/DROP re-run against changed state is how one failure
        # becomes two.
        max_attempts = 1
    retry_on: list[str] = retry.get("retry_on", [])
    pattern_absent: str = retry.get("pattern_absent", "")
    delay_s: float = retry.get("delay_s", 1.0)

    exit_code: int = -1
    bytes_truncated: bool = False
    timed_out: bool = False
    redactions: int = 0
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    capped: str = ""
    truncated: bool = False
    attempt: int = 0
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            exit_code, raw = _capture(cmd, stream, timeout, cwd, child_env)
        except subprocess.TimeoutExpired:
            timed_out = True
            last_error = f"Command timed out after {timeout}s (attempt {attempt})"
            if attempt < max_attempts:
                time.sleep(delay_s)
                continue
            break

        filtered = _filter(raw, filter_mode, filter_expr)
        capped, truncated = _cap(filtered, max_lines)
        capped, bytes_truncated = _cap_bytes(capped, max_bytes)
        if redact_on:
            capped, redactions = redact(capped, redact_extra)

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

    result = {
        "exit_code": exit_code,
        "filtered_output": capped,
        "purpose": purpose,
        "truncated": truncated,
        "bytes_truncated": bytes_truncated,
        "timed_out": timed_out,
        "attempt_count": attempt,
        "duration_s": round(time.monotonic() - started, 3),
        "classification": declared,
        "redactions_applied": redactions,
        "command_sha256": _command_hash(cmd),
        "started_at": started_at,
        "error": last_error,
    }
    _audit(
        outcome="executed",
        declaration=declaration,
        declared=declared,
        detected=detected,
        result=result,
    )
    return result
