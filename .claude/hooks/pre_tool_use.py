#!/usr/bin/env python3
"""
PreToolUse hook: bind every session to the project's command-execution rules.

CLAUDE.md states three hard rules about shell commands -- bounded output,
declared effect class, never leak a secret into context. A rule that lives
only in prose is a rule that decays: a future session skims, a summarised
context drops the paragraph, and an unbounded `cat` lands in the transcript.
This hook makes the rules mechanical, so they hold whether or not the model
happened to read them.

Contract (Claude Code PreToolUse):
  stdin : {"tool_name": str, "tool_input": {...}, ...}
  stdout: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "allow"|"deny"|"ask",
                                  "permissionDecisionReason": str}}
  exit 0 always -- the decision travels in the payload, not the exit code, so
  that a bug in this hook degrades to "allowed" rather than bricking every
  Bash call in the session.

Enforcement level comes from config/command_policy.json, overridable for one
session with TB_COMMAND_POLICY=block|warn|off. That override is the rollback
path: a hook that cannot be turned off is an outage waiting to happen.

Classification is imported from common/command_schema.py -- one implementation
shared with shell_exec.run() and the test suite, so the hook can never drift
into blocking something the runtime allows, or vice versa.
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

# The hook runs as a bare subprocess, not under the project's import path.
sys.path.insert(0, str(PROJECT_DIR))

try:
    from common.command_schema import classify
except Exception:  # pragma: no cover - defended below by _fail_open
    classify = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Decision plumbing
# --------------------------------------------------------------------------


def _emit(decision: str, reason: str = "") -> None:
    """Write the hook decision and exit 0."""
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
    sys.exit(0)


def _fail_open(note: str) -> None:
    """
    Allow the call when the hook itself cannot decide.

    A policy hook that fails closed on its own bug blocks all work and looks
    like a broken environment. It fails open and says so on stderr, which
    reaches the transcript without denying the call.
    """
    print(f"[pre_tool_use] degraded, allowing: {note}", file=sys.stderr)
    _emit("allow")


def _load_policy() -> dict[str, Any]:
    with POLICY_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _enforcement(policy: dict[str, Any]) -> str:
    level = os.environ.get("TB_COMMAND_POLICY", "").strip().lower()
    if level in {"block", "warn", "off"}:
        return level
    configured = str(policy.get("enforcement", "block")).lower()
    return configured if configured in {"block", "warn", "off"} else "block"


# --------------------------------------------------------------------------
# Command shape analysis
# --------------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;")

# Shells and interpreters whose heredoc body is executed rather than stored.
# For these the body must still be analysed; for everything else the body is
# data (a file being written) and analysing it produces false positives -- a
# test that quotes a blocked pattern is not the same as running one.
_INTERPRETERS = frozenset(
    {"bash", "sh", "zsh", "ksh", "dash", "python", "python3", "node", "perl", "ruby", "php"}
)

_HEREDOC_START = re.compile(r"""<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1""")


def _strip_heredoc_bodies(command: str) -> str:
    """
    Remove heredoc bodies that are written out rather than executed.

    A heredoc feeding a file write carries content, not a command line.
    Analysing that content flags any test or document that quotes a filtered
    pattern, which would make the policy unusable for the people writing the
    policy's own tests. Bodies fed to an interpreter are left in place,
    because those really do execute.
    """
    lines = command.split("\n")
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        out.append(line)
        match = _HEREDOC_START.search(line)
        if not match:
            idx += 1
            continue

        if _head_word(line) in _INTERPRETERS:
            idx += 1
            continue

        delimiter = match.group(2)
        idx += 1
        while idx < len(lines) and lines[idx].strip() != delimiter:
            idx += 1
        if idx < len(lines):
            idx += 1  # consume the closing delimiter
    return "\n".join(out)


def _is_write_not_read(segment: str) -> bool:
    """
    True when a nominally-reading command is actually writing a file.

    A redirect or heredoc means the segment produces no transcript output at
    all; treating it as an unbounded read blocks a legitimate silent write.
    """
    return bool(re.search(r">>?\s*\S", segment)) or bool(_HEREDOC_START.search(segment))


def _segments(command: str) -> list[str]:
    """Split a command line into pipeline/sequence segments."""
    return [seg.strip() for seg in _SEGMENT_SPLIT.split(command) if seg.strip()]


def _head_word(segment: str) -> str:
    """
    First real word of a segment, skipping env-var assignments and sudo.

    `FOO=1 sudo cat x` must be recognised as `cat`, not as `FOO=1`.
    """
    for token in segment.split():
        if (
            "=" in token
            and not token.startswith("-")
            and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token)
        ):
            continue
        if token in {"sudo", "env", "command", "time", "nohup"} and token != segment.split()[-1]:
            continue
        return token.rsplit("/", 1)[-1]
    return ""


def _is_bounded(command: str, patterns: list[str]) -> bool:
    """True when the command carries an explicit small output bound."""
    return any(re.search(p, command) for p in patterns)


def _oversized_bounds(command: str, cfg: dict[str, Any], limit: int) -> list[int]:
    """
    Find declared output bounds that exceed the per-fetch limit.

    A bound that is present but large is not a bound: `head -100` puts a
    hundred lines in context exactly as `cat` would. Returns every offending N
    so the message can name the actual number the caller wrote.
    """
    found: list[int] = []
    for pattern in cfg.get("oversized_bound_patterns", []):
        for match in re.finditer(pattern, command, re.IGNORECASE):
            value = int(match.group(1))
            if value > limit:
                found.append(value)
    byte_limit = int(cfg.get("max_declared_bytes", 2000))
    for pattern in cfg.get("oversized_byte_patterns", []):
        for match in re.finditer(pattern, command, re.IGNORECASE):
            if int(match.group(1)) > byte_limit:
                # Reported in line units so the caller gets one consistent
                # message; the byte figure itself is in the reason text.
                found.append(int(match.group(1)))
    for pattern in cfg.get("oversized_range_patterns", []):
        for match in re.finditer(pattern, command):
            start, end = int(match.group(1)), int(match.group(2))
            span = end - start + 1
            if span > limit:
                found.append(span)
    return found


def _needs_bound(segment: str, cfg: dict[str, Any]) -> bool:
    """
    True when a segment is an exempt command used in an unbounded subcommand.

    `git` as a whole is exempt so that `git status` and `git add` are not
    nagged, but `git log` with no -n pages the entire history into context.
    """
    required = cfg.get("bounded_required_subcommands", {})
    tokens = [t for t in segment.split() if not t.startswith("-")]
    if len(tokens) < 2:
        return False
    head = tokens[0].rsplit("/", 1)[-1]
    return tokens[1] in required.get(head, [])


def _violations(command: str, policy: dict[str, Any]) -> list[str]:
    """
    Collect every rule this command breaks, most severe first.

    Returns an empty list when the command is acceptable. Reasons are written
    for the reader who has to fix the command, so each one names the concrete
    replacement rather than restating the rule.
    """
    problems: list[str] = []
    # Heredoc bodies destined for a file are content, not commands.
    command = _strip_heredoc_bodies(command)

    # 1. Secrets ---------------------------------------------------------
    secret_cfg = policy.get("secret_echo", {})
    if secret_cfg.get("enabled", True):
        for pattern in secret_cfg.get("patterns", []):
            if re.search(pattern, command, re.IGNORECASE):
                problems.append(
                    "This command would print credentials into the transcript. "
                    "Read the value in Python and use it without echoing it, or "
                    'check only for presence (e.g. `test -n "$VAR" && echo set`).'
                )
                break

    # 2. Destructive effects ---------------------------------------------
    destructive_cfg = policy.get("destructive", {})
    if destructive_cfg.get("enabled", True) and classify is not None:
        marker = destructive_cfg.get("allow_marker", "TB_DESTRUCTIVE_OK=1")
        if classify(command) == "destructive" and marker not in command:
            problems.append(
                "Destructive command. Run it through common/shell_exec.run() with "
                "classification='destructive' and confirm_destructive=True so the "
                f"authorization is recorded, or prefix it with {marker} once a human "
                "has explicitly approved this specific action."
            )

    # 3. Unbounded output -------------------------------------------------
    bounded_cfg = policy.get("bounded_output", {})
    if bounded_cfg.get("enabled", True):
        limit = int(bounded_cfg.get("max_declared_lines", 5))
        unbounded = set(bounded_cfg.get("unbounded_commands", []))
        exempt = set(bounded_cfg.get("exempt_commands", []))
        patterns = list(bounded_cfg.get("bounded_flag_patterns", []))

        segs = _segments(command)
        # Only the last segment's output reaches the transcript; anything
        # earlier is consumed by the next stage of the pipeline.
        tail_seg = segs[-1] if segs else command
        head = _head_word(tail_seg)
        reader_anywhere = any(_head_word(seg) in unbounded for seg in segs)

        needs_bound = (
            head in unbounded
            or (reader_anywhere and head not in exempt)
            or any(_needs_bound(seg, bounded_cfg) for seg in segs)
        ) and not _is_write_not_read(tail_seg)
        if needs_bound and not _is_bounded(command, patterns):
            problems.append(
                f"Unbounded read: `{head}` can emit an entire file, tree or history "
                f"into context. Project directive is at most {limit} lines per fetch. "
                f"Use `sed -n '1,{limit}p' FILE`, `head -{limit}`, "
                f"`grep -m {limit} PATTERN FILE` or `-n {limit}`, then request the "
                f"next {limit} lines in a separate call."
            )

        oversized = _oversized_bounds(command, bounded_cfg, limit)
        if oversized:
            problems.append(
                f"Output bound of {max(oversized)} lines exceeds the {limit}-line "
                f"per-fetch limit. Lower it to {limit} and page: read lines 1-{limit}, "
                f"then {limit + 1}-{2 * limit} in a separate call. Widening the first "
                "fetch is exactly what the directive forbids."
            )

    return problems


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # pragma: no cover - stdin is provided by the host
        _fail_open(f"could not read stdin: {exc}")
        return

    if not raw.strip():
        _fail_open("empty hook payload")
        return

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_open(f"malformed hook payload: {exc}")
        return

    if event.get("tool_name") != "Bash":
        _emit("allow")
        return

    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command.strip():
        _emit("allow")
        return

    try:
        policy = _load_policy()
    except FileNotFoundError:
        _fail_open(f"policy file missing: {POLICY_PATH}")
        return
    except json.JSONDecodeError as exc:
        _fail_open(f"policy file is not valid JSON: {exc}")
        return

    level = _enforcement(policy)
    if level == "off":
        _emit("allow")
        return

    try:
        problems = _violations(command, policy)
    except re.error as exc:
        _fail_open(f"invalid regex in {POLICY_PATH.name}: {exc}")
        return

    if not problems:
        _emit("allow")
        return

    reason = " ".join(problems)
    if level == "warn":
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")
        return

    _emit("deny", reason)


if __name__ == "__main__":
    main()
