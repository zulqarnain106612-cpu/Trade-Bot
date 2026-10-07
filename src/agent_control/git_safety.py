"""
Guarded Git operations -- what must not run without an explicit, recorded reason.

``common.command_schema`` already refuses the irreversible forms (``reset
--hard``, ``clean -f``, ``push --force``) without the human approval marker.
This module covers the next ring out: operations that are recoverable in
principle but are exactly how uncommitted work and branch state get lost in a
long session -- ``checkout``, ``switch``, ``restore``, ``reset``, ``rebase``,
``merge``, ``pull`` (a merge or rebase in disguise), ``cherry-pick``,
``stash``, ``clean`` and every force push, ``--force-with-lease`` included.

While an agent-control task is active in a worktree, each such operation
needs a single-use, expiring authorization recorded against the task, and
recording one captures the pre-mutation Git snapshot as a checkpoint. A vague
instruction ("clean this up", "go back") authorizes nothing: the only thing
that does is an authorization naming the operation and the reason.

Detection splits the command the way a shell would -- control operators,
quoting, ``bash -c``, ``env``/``sudo``/``timeout`` wrappers and ``git -C``
style global options -- and drops heredoc bodies, so a commit message that
mentions "git merge" is not mistaken for one. Where the command cannot be
tokenised (an unbalanced quote) detection falls back to a conservative
pattern scan: a false positive costs one authorization, a false negative
costs work.

Registry: GOV-061 (config/quality_registry.json).
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.command_schema import redact
from src.agent_control.model import Authorization, ManifestError, TaskRun, next_id

FORCE_PUSH = "push-force"
GUARDED_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "checkout",
        "switch",
        "restore",
        "reset",
        "rebase",
        "merge",
        "pull",
        "cherry-pick",
        "stash",
        "clean",
    }
)
GUARDED_OPERATIONS: frozenset[str] = GUARDED_SUBCOMMANDS | {FORCE_PUSH}

DEFAULT_TTL_S = 900
MAX_TTL_S = 3600

# Subcommand forms that only read.
_READ_ONLY_FORMS: dict[str, frozenset[str]] = {"stash": frozenset({"list", "show"})}
_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nice", "nohup", "time", "timeout"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_PUNCTUATION = frozenset("();<>|&")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_FALLBACK = re.compile(
    r"\bgit\b[^\n;&|]*?"
    r"\b(checkout|switch|restore|reset|rebase|merge|pull|cherry-pick|stash|clean)\b"
    r"|\bgit\b[^\n;&|]*?\bpush\b[^\n;&|]*?(--force|\s-[a-zA-Z]*f|\s\+)"
)
_MAX_DEPTH = 3


@dataclass(frozen=True, slots=True)
class GitInvocation:
    subcommand: str
    args: tuple[str, ...]


def _in_quotes(line: str, index: int) -> bool:
    quote = ""
    escaped = False
    for char in line[:index]:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
    return bool(quote)


def _heredoc_markers(line: str) -> list[tuple[str, bool]]:
    markers: list[tuple[str, bool]] = []
    for match in _HEREDOC.finditer(line):
        start = match.start()
        if (start > 0 and line[start - 1] == "<") or _in_quotes(line, start):
            continue
        markers.append((match.group(3), match.group(1) == "-"))
    return markers


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are data fed to a command, never commands."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            delimiter, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
            continue
        kept.append(line)
        pending.extend(_heredoc_markers(line))
    return "\n".join(kept)


def _join_lines(command: str) -> str:
    """Unquoted newlines become ``;`` and backslash-newline continues the line."""
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            pair = command[index : index + 2]
            out.append("" if pair == "\\\n" else pair)
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "\n":
            char = " ; "
        out.append(char)
        index += 1
    return "".join(out)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_separator(token: str) -> bool:
    # shlex groups adjacent punctuation, so ")&&" arrives as one token. Any
    # all-punctuation token is a command boundary unless it is a redirection
    # (">", ">>", ">&", "<", "<&", ...), which only ever starts with < or >.
    return bool(token) and set(token) <= _PUNCTUATION and token[0] not in "<>"


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if _is_separator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _invocations(command: str, depth: int) -> list[GitInvocation]:
    found: list[GitInvocation] = []
    for words in _segments(_tokens(_join_lines(strip_heredocs(command)))):
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        head = os.path.basename(words[0])
        if head in _SHELLS or head == "eval":
            if depth < _MAX_DEPTH:
                found.extend(_invocations(_nested_script(head, words), depth + 1))
            continue
        if head in _WRAPPERS:
            words = next(
                (words[i:] for i, w in enumerate(words) if os.path.basename(w) == "git"), []
            )
        if not words or os.path.basename(words[0]) != "git":
            continue
        index = 1
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in _GLOBAL_OPTIONS_WITH_VALUE else 1
        if index < len(words):
            found.append(GitInvocation(words[index], tuple(words[index + 1 :])))
    return found


def _nested_script(head: str, words: list[str]) -> str:
    if head == "eval":
        return " ".join(words[1:])
    for index, word in enumerate(words[1:-1], start=1):
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            return words[index + 1]
    return ""


def git_invocations(command: str) -> list[GitInvocation]:
    """Every ``git`` command a shell would run for ``command``."""
    return _invocations(command, 0)


def _is_force_push(args: Iterable[str]) -> bool:
    for arg in args:
        if arg in ("--force", "--force-with-lease", "--force-if-includes"):
            return True
        if arg.startswith("--force-with-lease="):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return True
        if arg.startswith("+") and len(arg) > 1:
            return True
    return False


def operation_of(invocation: GitInvocation) -> str | None:
    sub = invocation.subcommand
    if sub == "push":
        return FORCE_PUSH if _is_force_push(invocation.args) else None
    if sub not in GUARDED_SUBCOMMANDS:
        return None
    read_only = _READ_ONLY_FORMS.get(sub, frozenset())
    if invocation.args and invocation.args[0] in read_only:
        return None
    return sub


def guarded_operations(command: str) -> list[str]:
    """Guarded operations ``command`` would perform, in order, without repeats."""
    try:
        operations = [operation_of(inv) for inv in git_invocations(command)]
    except ValueError:
        operations = []
        for match in _FALLBACK.finditer(command):
            operations.append(match.group(1) or FORCE_PUSH)
    return list(dict.fromkeys(op for op in operations if op))


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def authorize(
    task: TaskRun,
    operation: str,
    reason: str,
    *,
    checkpoint_id: str,
    now: datetime,
    ttl_s: int = DEFAULT_TTL_S,
) -> Authorization:
    """Record a single-use authorization for one guarded operation."""
    if operation not in GUARDED_OPERATIONS:
        known = ", ".join(sorted(GUARDED_OPERATIONS))
        raise ManifestError(f"{operation!r} is not a guarded operation (known: {known})")
    if not reason.strip():
        raise ManifestError("an authorization needs a reason")
    if not 1 <= ttl_s <= MAX_TTL_S:
        raise ManifestError(f"ttl must be between 1 and {MAX_TTL_S} seconds")
    grant = Authorization(
        authorization_id=next_id("AU", (a.authorization_id for a in task.authorizations)),
        operation=operation,
        reason=reason.strip(),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
        checkpoint_id=checkpoint_id,
    )
    task.authorizations.append(grant)
    return grant


def _available(task: TaskRun, operation: str, now: datetime) -> Authorization | None:
    for grant in task.authorizations:
        if (
            grant.operation == operation
            and grant.consumed_at is None
            and _parse(grant.expires_at) > now
        ):
            return grant
    return None


def consume(task: TaskRun, operations: Iterable[str], command: str, *, now: datetime) -> list[str]:
    """
    Spend one authorization per operation. All or nothing: if any operation
    lacks a live authorization nothing is consumed and the missing ones are
    returned.
    """
    granted: list[Authorization] = []
    missing: list[str] = []
    for operation in dict.fromkeys(operations):
        grant = _available(task, operation, now)
        if grant is None:
            missing.append(operation)
        else:
            granted.append(grant)
    if missing:
        return missing
    recorded = redact(command)[0][:500]
    for grant in granted:
        grant.consumed_at = now.isoformat()
        grant.consumed_by = recorded
    return []
