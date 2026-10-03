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

try:
    # Shared with shell_exec.run() so the hook and the runtime cannot disagree
    # about what counts as reading CI data. When the project is not importable
    # -- a bare session, a detached checkout -- _ci_log_access falls back to
    # the equivalent patterns in config/command_policy.json, which is why they
    # are duplicated there rather than only here.
    from common.command_schema import is_ci_log_access as _shared_is_ci_log_access
except Exception:  # pragma: no cover - policy-file fallback covers this
    _shared_is_ci_log_access = None  # type: ignore[assignment]


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

# Separators that start a new command, each with its own output destination.
_COMMAND_SPLIT = re.compile(r"\|\||&&|;")

# Within one command, the stages of a pipeline: only the last stage's output
# is seen by the caller, the rest are consumed by the next stage.
_STAGE_SPLIT = re.compile(r"(?<!\|)\|(?!\|)")

# Shells whose heredoc body is itself a command line, and so must still be
# analysed. Deliberately shells only. A Python, Node or Perl heredoc body is
# executed too, but it is not a shell command line: matching shell patterns
# against it flags any script whose source merely contains the word for a
# filtered command, and catches nothing real, since textual classification
# never saw inside an interpreter in the first place (see classify()). For
# every other receiver the body is data being written to a file.
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "ksh", "dash"})

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

        if _head_word(line) in _SHELL_INTERPRETERS:
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

    A redirect means the segment produces no transcript output at all, so
    treating it as an unbounded read would block a legitimate silent write.

    A heredoc on its own does NOT qualify. ``cat > notes.md <<EOF`` writes a
    file and is silent because of the redirect, but ``python3 - <<PY`` feeds a
    program that can print whatever it likes -- and an interpreter fed a script
    was the last route by which an unbounded read could still reach the
    session. The heredoc *body* is still stripped before any rule reads the
    command, so its text is never mistaken for a command.
    """
    return bool(re.search(r">>?\s*\S", segment))


def _split_top_level(text: str, operators: tuple[str, ...]) -> list[str]:
    """
    Split on shell operators that are not inside quotes.

    Splitting with a plain regex tears a quoted script in half: the `&&` in
    ``awk 'NR<=190 && /x/ {...}' f | head -2`` is awk source, not a command
    separator, and the fragment left of it carries no bound. The result was a
    refusal for a command that was correctly bounded all along -- and a guard
    that refuses correct commands is a guard someone switches off.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            buf.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                buf.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            buf.append(char)
            buf.append(text[index + 1])
            index += 2
            continue
        # `||` is a command separator, never a pipeline stage boundary, so a
        # pipe split steps over it rather than cutting it in two.
        if operators == ("|",) and text.startswith("||", index):
            buf.append("||")
            index += 2
            continue
        separator = next((op for op in operators if text.startswith(op, index)), "")
        if separator:
            parts.append("".join(buf))
            buf = []
            index += len(separator)
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _commands(command_line: str) -> list[str]:
    """Split a command line into independently-output-producing commands."""
    return _split_top_level(command_line, ("&&", "||", ";"))


def _stages(command: str) -> list[str]:
    """Split one command into its pipeline stages."""
    return _split_top_level(command, ("|",))


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


def _live_monitoring(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a command is a live CI watch, else "".

    A per-call line bound cannot cap a stream that never ends: `gh run watch`,
    `tail -f` and a `while true` poll loop each emit for as long as CI runs,
    and every line lands in context. Bulk log retrieval is not banned here --
    it is capped by bounded_output, so the few lines that explain a failure
    stay reachable. Only the open-ended form is refused.
    """
    cfg = policy.get("ci_observability", {})
    if not cfg.get("enabled", True):
        return ""
    for pattern in cfg.get("live_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return str(cfg.get("message", "CI live monitoring is disabled."))
    return ""


def _ci_log_access(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a command reads CI run data, else "".

    Distinct from :func:`_live_monitoring`, which refuses only the open-ended
    forms. This refuses *every* form: a run log, a job record, an annotation,
    an artifact, a check result, and dispatching a run in order to read what it
    prints. There is no line bound that makes it allowed, because the objection
    is not output size -- the pull-request notice already carries the status
    and the exact failing lines, so fetching is a worse route to the same
    answer that also costs context.

    The comment allowlist is checked first and wins. Closing the comment
    channel would leave no way at all to learn why a run failed, and a guard
    with no remaining route is a guard someone switches off.

    Shares :func:`~common.command_schema.is_ci_log_access` with the runtime
    where that import is available, so the hook and ``shell_exec.run()`` cannot
    disagree. The policy file's patterns are the fallback when this hook runs
    without the project importable, which is the case in a bare session.
    """
    cfg = policy.get("ci_log_access", {})
    if not cfg.get("enabled", True):
        return ""

    message = str(cfg.get("message", "CI log access is permanently disabled."))

    if _shared_is_ci_log_access is not None:
        return message if _shared_is_ci_log_access(command) else ""

    # Same three tiers as the shared implementation, in the same order.
    for pattern in cfg.get("hard_deny_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return message
    for pattern in cfg.get("allowed_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return ""
    for pattern in cfg.get("banned_patterns", []):
        if re.search(pattern, command, re.IGNORECASE):
            return message
    return ""


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

    # 3. Work GitHub already does ----------------------------------------
    # Ahead of the output rules and returning immediately: the objection is
    # not output size, so appending a line-bound refusal would suggest a
    # smaller local run was the fix. The fix is to push the branch.
    local_ci = _local_ci_work(command, policy)
    if local_ci:
        problems.append(local_ci)
        return problems

    # 4. CI run data ------------------------------------------------------
    # Before live monitoring and before the bounded-output rules, and it
    # returns immediately: this is not an output-size objection, so appending
    # a line-bound refusal would suggest a smaller bound would have worked.
    # Nothing about a CI log, job record, annotation, artifact or check result
    # is readable from here under any condition; the pull-request notice is
    # the channel, and it already carries the status and the failing lines.
    ci_logs = _ci_log_access(command, policy)
    if ci_logs:
        problems.append(ci_logs)
        return problems

    # 4b. Comment reads ----------------------------------------------------
    # The channel the CI rule leaves open is one comment wide. Returns
    # immediately: the objection is which comment was asked for, not how many
    # lines came back, and the bound rules below say nothing about that.
    comments = _comment_read(command, policy)
    if comments:
        problems.append(comments)
        return problems

    # 5. Live CI monitoring -----------------------------------------------
    # A live watch is refused outright, and the bounded-output rules below are
    # not consulted: its message already carries the line directive, and
    # appending the bound refusal to it would say the same thing twice.
    live = _live_monitoring(command, policy)
    if live:
        problems.append(live)
        return problems

    # 6. GitHub reads ------------------------------------------------------
    # Before the thirty-line rule, because the figure that applies to a
    # GitHub report is two, and a refusal naming thirty would be wrong.
    github = _github_read_bound(command, policy)
    if github:
        problems.append(github)
        return problems

    # 7. Direct file reads -------------------------------------------------
    # Also before the thirty-line rule. Someone who ran `cat` needs to be
    # told to search, not told a number.
    direct = _direct_file_read(command, policy)
    if direct:
        problems.append(direct)
        return problems

    # 8. Unbounded output -------------------------------------------------
    bounded_cfg = policy.get("bounded_output", {})
    if bounded_cfg.get("enabled", True):
        limit = int(bounded_cfg.get("max_declared_lines", 30))
        # One line leaves the hook on a bound violation, by configuration.
        # A longer explanation is itself context spend on a call that was
        # refused precisely to protect context.
        refusal = str(
            bounded_cfg.get(
                "refusal_message",
                f"only <={limit} lines are allowed,run command for minimum "
                "line which can make you understand the failure",
            )
        )
        unbounded = set(bounded_cfg.get("unbounded_commands", []))
        exempt = set(bounded_cfg.get("exempt_commands", []))
        # An exempt command is never itself an unbounded reader; the
        # bounded_required_subcommands map is what re-arms specific
        # subcommands of one, such as git log or kubectl logs.
        unbounded -= exempt
        patterns = list(bounded_cfg.get("bounded_flag_patterns", []))

        for cmd in _commands(command):
            stages = _stages(cmd)
            if not stages:
                continue

            # A command whose final stage redirects to a file or feeds a
            # heredoc produces no transcript output, so no bound applies.
            if _is_write_not_read(stages[-1]):
                continue

            offender = ""
            for stage in stages:
                stage_head = _head_word(stage)
                if stage_head in unbounded or _needs_bound(stage, bounded_cfg):
                    offender = stage_head
                    break

            if not offender or _is_bounded(cmd, patterns):
                continue

            problems.append(refusal)
            break

        oversized = _oversized_bounds(command, bounded_cfg, limit)
        if oversized:
            problems.append(refusal)

    return problems


def _local_ci_work(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a command runs CI's job here, else "".

    Detection is on the executable at the head of a pipeline segment and never
    on a substring of the line, so ``grep -n pytest tests/foo.py`` stays a
    search. ``python -m <module>`` is matched separately because its head word
    is the interpreter, not the tool being run.

    The message is one short phrase by design: the replacement is not a smaller
    local command, it is pushing the branch, so there is nothing to tune.
    """
    cfg = policy.get("local_ci_work", {})
    if not cfg.get("enabled", True):
        return ""

    message = str(cfg.get("message", "use github platform"))

    executables = set(cfg.get("blocked_executables", []))
    subcommands = {k: set(v) for k, v in cfg.get("blocked_subcommands", {}).items()}
    modules = set(cfg.get("blocked_python_modules", []))
    runners = set(cfg.get("script_runners", []))
    scripts = list(cfg.get("blocked_script_patterns", []))

    for cmd in _commands(command):
        for stage in _stages(cmd):
            head = _head_word(stage)
            if not head:
                continue
            if head in executables:
                return message
            # A script pattern applies only where something is actually being
            # run: an interpreter, a package runner, or the script itself.
            # `grep -n qe_gate.py scripts/` names the script without running
            # it, and refusing that would make the repository unsearchable.
            if head in runners or head.endswith((".py", ".sh")):
                for pattern in scripts:
                    if re.search(pattern, stage, re.IGNORECASE):
                        return message
            tokens = stage.split()
            if head.startswith("python"):
                for index, token in enumerate(tokens[:-1]):
                    if token in {"-m", "--module"} and tokens[index + 1] in modules:
                        return message
            allowed = subcommands.get(head)
            if allowed:
                positional = [t for t in tokens[1:] if not t.startswith("-")]
                if positional and positional[0] in allowed:
                    return message
    return ""


def _comment_read(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a comment read is not the latest one.

    The pull-request comment channel stays open -- closing it would leave no
    way at all to learn why a run failed -- but it is narrowed to a single
    comment. A query against the whole ``comments`` array pulls every notice
    ever posted into one call, so the command has to name the last one itself.

    ``head -1`` is not a selector: it returns the oldest comment.
    """
    cfg = policy.get("comment_read", {})
    if not cfg.get("enabled", True):
        return ""
    if not any(re.search(p, command, re.IGNORECASE) for p in cfg.get("patterns", [])):
        return ""
    if any(
        re.search(selector, command, re.IGNORECASE)
        for selector in cfg.get("latest_selectors", [])
    ):
        return ""
    return str(cfg.get("message", "only the latest comment may be read."))


def _github_read_bound(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a GitHub read is not capped at two lines.

    Anything that reports on GitHub -- a CI notice, a PR status, a run, a
    workflow, a runner, ``git status``, ``git log``, ``git diff`` -- returns an
    open-ended report whose useful part is one or two lines, so the cap is
    declared in the command and the truncation happens before the output
    reaches context rather than after.

    Checked before the thirty-line rule so the caller is told the figure that
    actually applies. Redirects are deliberately *not* exempt here: writing the
    report to a file and reading the file back would be the same unbounded read
    with one extra step.

    Mutating commands are not listed at all. ``git commit``, ``git push`` and
    ``gh pr create`` are not reads, and piping them through ``head`` would hide
    the failures that matter most.
    """
    cfg = policy.get("github_read_bound", {})
    if not cfg.get("enabled", True):
        return ""

    # Matched per pipeline stage and only against a stage whose executable is
    # one of the clients, never against the whole line: `grep -n "git status"`
    # mentions a GitHub read without performing one, and a search that reports
    # itself as a read is the fastest way to a guard nobody keeps.
    clients = set(cfg.get("read_clients", []))
    patterns = list(cfg.get("read_patterns", []))
    if not any(
        _head_word(stage) in clients
        and any(re.search(pattern, stage, re.IGNORECASE) for pattern in patterns)
        for cmd in _commands(command)
        for stage in _stages(cmd)
    ):
        return ""

    message = str(cfg.get("message", "only 2 lines reading is allowed"))
    limit = int(cfg.get("max_declared_lines", 2))

    if not _is_bounded(command, list(cfg.get("bounded_flag_patterns", []))):
        return message

    merged = dict(policy.get("bounded_output", {}))
    merged.update(cfg)
    if _oversized_bounds(command, merged, limit):
        return message
    return ""


def _direct_file_read(command: str, policy: dict[str, Any]) -> str:
    """
    Return the refusal message when a command opens a file whole, else "".

    A file is searched, not opened: ``grep -n`` answers in one line what
    ``cat`` answers in four hundred, and an edit needs the exact block rather
    than everything around it. Reading is allowed in the bounded shape that
    follows a search, which is why a bounded pipeline passes untouched.

    Distinct from the thirty-line rule only in what it says. The caller who
    ran ``cat`` needs to be told to search, not told a number.
    """
    cfg = policy.get("direct_file_read", {})
    if not cfg.get("enabled", True):
        return ""

    readers = set(cfg.get("whole_file_readers", []))
    patterns = list(policy.get("bounded_output", {}).get("bounded_flag_patterns", []))

    for cmd in _commands(command):
        stages = _stages(cmd)
        if not stages or _is_write_not_read(stages[-1]):
            continue
        if not any(_head_word(stage) in readers for stage in stages):
            continue
        if _is_bounded(cmd, patterns):
            continue
        return str(cfg.get("message", "direct file reading is not allowed."))
    return ""


# --------------------------------------------------------------------------
# Per-file session read budget
# --------------------------------------------------------------------------

_RANGE_READERS = frozenset(
    {"sed", "head", "tail", "awk", "nl", "cat", "less", "more", "bat", "tac"}
)


def _declared_lines(command: str, policy: dict[str, Any]) -> int:
    """
    The largest line count this command declares, or 0 when it declares none.

    Bookkeeping only, so the caller substitutes the per-call ceiling for 0: a
    read whose bound could not be parsed is not a free one.
    """
    cfg = policy.get("bounded_output", {})
    best = 0
    for pattern in cfg.get("oversized_bound_patterns", []):
        for match in re.finditer(pattern, command, re.IGNORECASE):
            best = max(best, int(match.group(1)))
    for pattern in cfg.get("oversized_range_patterns", []):
        for match in re.finditer(pattern, command):
            best = max(best, int(match.group(2)) - int(match.group(1)) + 1)
    return best


def _read_paths(command: str) -> list[str]:
    """
    Project files this command reads by line range, project-relative.

    Only an argument that exists as a regular file inside the tree counts: a
    sed script, a flag, a glob or a path outside the project is not a file this
    budget protects. A search is not in :data:`_RANGE_READERS` on purpose --
    ``grep`` must stay the route that never runs out.
    """
    found: list[str] = []
    try:
        root = PROJECT_DIR.resolve()
    except OSError:  # pragma: no cover - unreadable project root
        return found
    # Heredoc bodies are content being written, not a command being run. Left
    # in, a document that merely contains a pipe produces a stage that looks
    # like `head -2 ...` and charges whatever path appears on a later line.
    for cmd in _commands(_strip_heredoc_bodies(command)):
        for stage in _stages(cmd):
            if _head_word(stage) not in _RANGE_READERS:
                continue
            # A stage that redirects into a file produces no transcript output,
            # so it is a write and costs no read budget.
            if _is_write_not_read(stage):
                continue
            for token in stage.split()[1:]:
                candidate = token.strip("'\"")
                if not candidate or candidate.startswith(("-", "$")):
                    continue
                try:
                    path = Path(candidate)
                    path = path if path.is_absolute() else root / path
                    resolved = path.resolve()
                    if not resolved.is_file():
                        continue
                    relative = str(resolved.relative_to(root))
                except (OSError, ValueError):
                    continue
                if relative not in found:
                    found.append(relative)
    return found


def _tool_read_paths(target: str) -> list[str]:
    """The project-relative form of a read tool's target, or nothing."""
    if not target:
        return []
    try:
        root = PROJECT_DIR.resolve()
        path = Path(target)
        path = path if path.is_absolute() else root / path
        resolved = path.resolve()
        if not resolved.is_file():
            return []
        return [str(resolved.relative_to(root))]
    except (OSError, ValueError):
        return []


def _budget_path(policy: dict[str, Any], session_id: str) -> Path:
    cfg = policy.get("direct_file_read", {}).get("paging", {})
    state_dir = PROJECT_DIR / str(cfg.get("state_dir", ".claude/.hook-state"))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) or "default"
    return state_dir / f"read-budget-{safe}.json"


def _budget_load(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _budget_verdict(
    paths: list[str],
    lines: int,
    policy: dict[str, Any],
    session_id: str,
) -> str:
    """
    Return the refusal message when a file's session budget is spent, else "".

    Thirty bounded reads walked down one file is a whole-file read with extra
    steps, so the per-call bound is not the only cap: a file may be opened a
    few times in a session, after which the answer has to come from a search.
    """
    cfg = policy.get("direct_file_read", {}).get("paging", {})
    if not cfg.get("enabled", True) or not paths:
        return ""
    max_reads = int(cfg.get("max_reads_per_file", 6))
    max_lines = int(cfg.get("max_lines_per_file", 120))
    state = _budget_load(_budget_path(policy, session_id))
    for relative in paths:
        entry = state.get(relative) or {}
        try:
            reads = int(entry.get("reads", 0)) + 1
            total = int(entry.get("lines", 0)) + max(lines, 0)
        except (TypeError, ValueError):
            continue
        if reads > max_reads or total > max_lines:
            return str(cfg.get("message", "this file's read budget is spent."))
    return ""


def _budget_record(
    paths: list[str],
    lines: int,
    policy: dict[str, Any],
    session_id: str,
) -> None:
    """
    Charge an allowed read to its files.

    Every failure here allows the call and says so on stderr: a budget that
    cannot be written must not become a reason that work stops.
    """
    cfg = policy.get("direct_file_read", {}).get("paging", {})
    if not cfg.get("enabled", True) or not paths:
        return
    path = _budget_path(policy, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _budget_load(path)
        for relative in paths:
            entry = state.get(relative)
            if not isinstance(entry, dict):
                entry = {"reads": 0, "lines": 0}
            entry["reads"] = int(entry.get("reads", 0)) + 1
            entry["lines"] = int(entry.get("lines", 0)) + max(lines, 0)
            state[relative] = entry
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - bookkeeping must never block
        print(f"[pre_tool_use] read budget not recorded: {exc}", file=sys.stderr)


def _read_tool_verdict(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    """
    Return the refusal message when a read tool call is not capped, else "".

    The limit is about how many lines reach the transcript, not about which
    tool fetched them, so every tool returning file or search content is held
    to it. A tool with a bound field must declare it at or under the cap; a
    tool with no bound field has no allowed form and is refused outright.

    Three shapes defeat a bound that is nominally present, and each is refused:
    a negative offset, which makes Desktop Commander read a tail of any length
    and ignore ``length`` entirely; ``isUrl``, which reads a URL in full
    whatever the bound says; and a second field that multiplies the output, as
    ``contextLines`` does for every match a search returns.
    """
    cfg = policy.get("direct_file_read", {})
    if not cfg.get("enabled", True):
        return ""

    if tool_name in set(cfg.get("blocked_read_tools", [])):
        return str(cfg.get("blocked_tool_message", "this tool returns 2+ lines."))

    spec = cfg.get("read_tools", {}).get(tool_name)
    if not isinstance(spec, dict):
        return ""

    message = str(cfg.get("message", "direct file reading is not allowed."))

    for field in spec.get("forbid_true", []):
        if bool(tool_input.get(field)):
            return message

    for field in spec.get("forbid_negative", []):
        try:
            if int(tool_input.get(field) or 0) < 0:
                return message
        except (TypeError, ValueError):
            return message

    for field, cap in (spec.get("also_max") or {}).items():
        if field not in tool_input:
            continue
        try:
            if int(tool_input[field]) > int(cap):
                return message
        except (TypeError, ValueError):
            return message

    try:
        declared = int(tool_input.get(str(spec.get("bound", "limit"))))
    except (TypeError, ValueError):
        return message
    return message if declared < 1 or declared > int(spec.get("max", 2)) else ""


def _read_tool_target(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    """The path a read tool was pointed at, for the per-file budget."""
    spec = policy.get("direct_file_read", {}).get("read_tools", {}).get(tool_name)
    if not isinstance(spec, dict):
        return ""
    field = spec.get("target")
    return str(tool_input.get(field) or "") if field else ""


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

    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = str(event.get("session_id") or "default")

    # A banned tool is refused before the policy is even consulted for shape:
    # Monitor is a live watch by construction, so there is no bounded form of
    # it to fall back to. Loaded first because this check is not about the
    # command string, which a non-Bash tool does not have.
    #
    # The file-reading tools are judged here too, on the same per-file budget
    # the Bash readers are charged against, so paging a file through Read is
    # not a way around paging it through sed.
    if tool_name != "Bash":
        try:
            policy = _load_policy()
        except Exception:
            _emit("allow")
            return

        ci_cfg = policy.get("ci_observability", {})
        if ci_cfg.get("enabled", True) and tool_name in set(ci_cfg.get("banned_tools", [])):
            _emit("deny", str(ci_cfg.get("message", "Live monitoring is disabled.")))

        level = _enforcement(policy)
        if level == "off":
            _emit("allow")
            return

        read_cfg = policy.get("direct_file_read", {})
        reason = _read_tool_verdict(tool_name, tool_input, policy)
        spec = read_cfg.get("read_tools", {}).get(tool_name)
        if not reason and isinstance(spec, dict):
            try:
                declared = int(tool_input.get(str(spec.get("bound", "limit"))) or 0)
            except (TypeError, ValueError):
                declared = int(read_cfg.get("max_declared_lines", 2))
            paths = _tool_read_paths(_read_tool_target(tool_name, tool_input, policy))
            reason = _budget_verdict(paths, declared, policy, session_id)
            if not reason:
                _budget_record(paths, declared, policy, session_id)

        if reason:
            if level == "warn":
                print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
                _emit("allow")
            _emit("deny", reason)
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
        paths = _read_paths(command)
        if paths:
            ceiling = int(policy.get("bounded_output", {}).get("max_declared_lines", 30))
            lines = _declared_lines(command, policy) or ceiling
            budget = _budget_verdict(paths, lines, policy, session_id)
            if budget:
                if level == "warn":
                    print(f"[pre_tool_use] policy warning: {budget}", file=sys.stderr)
                    _emit("allow")
                _emit("deny", budget)
            _budget_record(paths, lines, policy, session_id)
        _emit("allow")
        return

    deduped: list[str] = []
    for problem in problems:
        if problem not in deduped:
            deduped.append(problem)
    reason = " ".join(deduped)
    if level == "warn":
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")
        return

    _emit("deny", reason)


if __name__ == "__main__":
    main()
