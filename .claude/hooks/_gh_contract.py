#!/usr/bin/env python3
"""
Code-owned GitHub route contract: the single source of truth for which
GitHub MCP calls may execute, with which arguments, and how a write is
reduced to the candidates that actually changed.

Nothing in config/ can widen anything here. Configuration may only narrow.

Tool names below were verified by tools/list against the connected server
(https://api.githubcopilot.com/mcp/) on 2026-10-15; VERIFIED_SERVER_TOOLS is
the full 46-tool inventory of that session. A name absent from that set is
denied as unverified rather than assumed, so a server-side rename or a new
tool cannot silently open a route. tests/test_github_route_contract.py pins
this agreement.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GITHUB_REPOSITORY = "zulqarnain106612-cpu/Trade-Bot"
GITHUB_TOOL_PREFIX = "mcp__github__"

# Hard ceilings. Configuration may lower these; it can never raise them.
HARD_MAX_PER_PAGE = 20
HARD_MAX_NEW_FILE_LINES = 400
HARD_MAX_CHANGED_RATIO = 0.34
HARD_MAX_DELTA_BLOCKS = 20

DELTA_SENTINEL = "@@TB-DELTA v1@@"
_DELTA_BLOCK_RE = re.compile(
    r"<<<<<<< OLD\r?\n(?P<old>.*?)\r?\n======= NEW\r?\n(?P<new>.*?)\r?\n>>>>>>> END",
    re.DOTALL,
)

# The 46 tools the connected server actually exposes.
VERIFIED_SERVER_TOOLS = frozenset(
    {
        "add_comment_to_pending_review",
        "add_issue_comment",
        "add_reply_to_pull_request_comment",
        "create_branch",
        "create_or_update_file",
        "create_pull_request",
        "create_repository",
        "delete_file",
        "fork_repository",
        "get_commit",
        "get_file_contents",
        "get_label",
        "get_latest_release",
        "get_me",
        "get_release_by_tag",
        "get_tag",
        "get_team_members",
        "get_teams",
        "issue_read",
        "issue_write",
        "list_branches",
        "list_commits",
        "list_issue_fields",
        "list_issue_types",
        "list_issues",
        "list_pull_requests",
        "list_releases",
        "list_repository_collaborators",
        "list_tags",
        "merge_pull_request",
        "pull_request_read",
        "pull_request_review_write",
        "push_files",
        "request_copilot_review",
        "run_secret_scanning",
        "search_code",
        "search_commits",
        "search_issues",
        "search_pull_requests",
        "search_repositories",
        "search_users",
        "sub_issue_write",
        "ui_get",
        "update_issue_comment",
        "update_pull_request",
        "update_pull_request_branch",
    }
)

# Every verified tool that is deliberately refused, each with the reason the
# agent is shown. A verified tool missing from both this map and CONTRACTS is
# a contract gap and fails closed in unresolved_tools().
DENIED_TOOLS: dict[str, str] = {
    "push_files": "push_files carries no blob sha, so it is a blind multi-file overwrite with no delta check.",
    "delete_file": "File deletion is a destructive route and is denied.",
    "merge_pull_request": "Merging is a human decision; the agent may open and update a PR only.",
    "update_pull_request_branch": "Branch syncing rewrites the PR head outside the delta contract.",
    "pull_request_review_write": "Approving or reviewing a PR is a human governance act, not an agent action.",
    "add_comment_to_pending_review": "Pending-review authoring is part of the review route and is denied.",
    "add_reply_to_pull_request_comment": "Comment replies require reading arbitrary comment threads.",
    "update_issue_comment": "Editing existing comments is not a diagnostic route; the finalized report is stored in SHA-bound Check Runs.",
    "issue_write": "Issue mutation is outside the code-change route.",
    "sub_issue_write": "Issue-hierarchy mutation is outside the code-change route.",
    "create_repository": "Repository creation escapes the single-repository scope.",
    "fork_repository": "Forking escapes the single-repository scope.",
    "run_secret_scanning": "Secret-scanning results are a security report route, not an agent observation.",
    "request_copilot_review": "Requesting an external review actor is outside the agent's route.",
    "ui_get": "Undocumented UI surface; not an auditable data route.",
    "get_me": "Account identity is not needed for repository work.",
    "get_teams": "Organisation membership data is not needed for repository work.",
    "get_team_members": "Organisation membership data is not needed for repository work.",
    "list_repository_collaborators": "Collaborator lists are personal data and are not needed.",
    "search_users": "User search is personal data and is not needed.",
    "search_repositories": "Cross-repository search escapes the single-repository scope.",
    "list_issues": "Bulk issue listing is an unbounded report; search_issues with a scoped query is the route.",
    "list_issue_fields": "Issue metadata enumeration is an unbounded report.",
    "list_issue_types": "Issue metadata enumeration is an unbounded report.",
    "list_releases": "Release enumeration is an unbounded report.",
    "list_tags": "Tag enumeration is an unbounded report.",
    "get_latest_release": "Release metadata is not needed for repository work.",
    "get_release_by_tag": "Release metadata is not needed for repository work.",
    "get_tag": "Tag metadata is not needed for repository work.",
    "get_label": "Label metadata is not needed for repository work.",
}


@dataclass(frozen=True)
class Contract:
    """The argument contract for one allowed tool."""

    # Allowed values for a discriminating "method" parameter, if it has one.
    methods: frozenset[str] = frozenset()
    # Parameters that must be present and non-empty.
    required: tuple[str, ...] = ()
    # Parameters that must be absent (they widen the call beyond the contract).
    forbidden: tuple[str, ...] = ()
    # Enforce HARD_MAX_PER_PAGE on perPage and require it to be present.
    bounded_page: bool = False
    # Write semantics: this call mutates the repository.
    write: bool = False
    # A repo-relative path argument that must clear the protected-path check.
    path_arg: str = ""
    # Parameter values that are refused even on an allowed tool, because the
    # value itself selects a wider route than the tool name implies.
    denied_values: tuple[tuple[str, frozenset[str]], ...] = ()


CONTRACTS: dict[str, Contract] = {
    # ---- reads: targeted candidates only -------------------------------
    "search_code": Contract(required=("query",), bounded_page=True),
    "search_issues": Contract(required=("query",), bounded_page=True),
    "search_pull_requests": Contract(required=("query",), bounded_page=True),
    "search_commits": Contract(required=("query",), bounded_page=True),
    "get_file_contents": Contract(required=("path", "ref"), path_arg="path"),
    # detail=full_patch returns the whole commit patch, which is a full report.
    "get_commit": Contract(
        required=("sha",),
        bounded_page=True,
        denied_values=(("detail", frozenset({"full_patch"})),),
    ),
    "list_branches": Contract(bounded_page=True),
    "list_commits": Contract(bounded_page=True),
    "list_pull_requests": Contract(bounded_page=True),
    # get_status and get_check_runs are CI run data; get_diff, get_files and
    # get_commits are whole-PR reports; get_comments, get_reviews and
    # get_review_comments expose unrelated comment bodies. The sole local CI
    # report route is ./scripts/prfail --pr <PR_NUMBER>; it reads only the
    # finalized Check Run manifest and digest-verified pages.
    "pull_request_read": Contract(methods=frozenset({"get"})),
    "issue_read": Contract(methods=frozenset({"get"})),
    # ---- writes: delta-checked ----------------------------------------
    # Real parameters are branch and from_branch; branch_name/base_ref do not
    # exist on this server and reading them denied every legitimate call.
    "create_branch": Contract(required=("branch",), write=True),
    "create_or_update_file": Contract(
        required=("path", "branch", "content", "message"),
        write=True,
        path_arg="path",
    ),
    "create_pull_request": Contract(required=("title", "head", "base"), write=True),
    # state=closed closes a PR and reviewers assigns people: both are human
    # governance acts rather than code changes.
    "update_pull_request": Contract(
        required=("pullNumber",), forbidden=("state", "reviewers"), write=True
    ),
    # comment_id edits an existing comment; reaction is not a message. Neither
    # operation is a CI diagnostic route.
    "add_issue_comment": Contract(
        required=("issue_number", "body"), forbidden=("comment_id", "reaction"), write=True
    ),
}


def unresolved_tools() -> frozenset[str]:
    """Verified server tools that neither contract nor denial covers."""
    return frozenset(VERIFIED_SERVER_TOOLS - set(CONTRACTS) - set(DENIED_TOOLS))


# ---------------------------------------------------------------------------
# Blob state: what the agent was actually shown, kept off the model's context.
# ---------------------------------------------------------------------------


@dataclass
class BlobStore:
    """
    Full fetched file bodies, on disk, keyed by path and ref.

    PostToolUse writes an entry when a get_file_contents call succeeds;
    PreToolUse reads it to reconstruct a delta write and to reject a write
    that rewrites more than it changed. The model never receives these bodies
    in full: observation_gate bounds what reaches context.
    """

    root: Path

    def _key(self, path: str, ref: str) -> Path:
        digest = hashlib.sha256(f"{ref}\x00{path}".encode()).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def put(self, path: str, ref: str, content: str, sha: str = "") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = {
            "path": path,
            "ref": ref,
            "sha": sha,
            "content": content,
            "lines": len(content.splitlines()),
            "fetched_at": int(time.time()),
        }
        target = self._key(path, ref)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(target)

    def get(self, path: str, ref: str) -> dict[str, Any] | None:
        try:
            with self._key(path, ref).open(encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def any_ref(self, path: str) -> dict[str, Any] | None:
        """The most recently fetched body for a path, whatever ref it came from."""
        newest: dict[str, Any] | None = None
        try:
            entries = list(self.root.glob("*.json"))
        except OSError:
            return None
        for entry in entries:
            try:
                with entry.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or data.get("path") != path:
                continue
            if newest is None or int(data.get("fetched_at", 0)) > int(newest.get("fetched_at", 0)):
                newest = data
        return newest


# ---------------------------------------------------------------------------
# Delta writes
# ---------------------------------------------------------------------------


@dataclass
class DeltaResult:
    ok: bool
    content: str = ""
    reason: str = ""
    blocks: int = 0
    changed_ratio: float = 0.0
    applied: tuple[str, ...] = field(default_factory=tuple)


def is_delta_payload(content: object) -> bool:
    return isinstance(content, str) and content.lstrip().startswith(DELTA_SENTINEL)


def apply_delta(base: str, payload: str) -> DeltaResult:
    """
    Apply anchored OLD/NEW blocks to ``base``.

    Each OLD anchor must appear exactly once in the current text, so a block
    can never land in the wrong place and an ambiguous anchor is refused
    instead of guessed.
    """
    blocks = _DELTA_BLOCK_RE.findall(payload)
    if not blocks:
        return DeltaResult(
            False,
            reason=(
                "Delta payload contains no well-formed block. Each change is "
                "'<<<<<<< OLD' / '======= NEW' / '>>>>>>> END'."
            ),
        )
    if len(blocks) > HARD_MAX_DELTA_BLOCKS:
        return DeltaResult(
            False,
            reason=f"A delta write carries at most {HARD_MAX_DELTA_BLOCKS} blocks; this one has {len(blocks)}.",
        )

    text = base
    applied: list[str] = []
    for index, (old, new) in enumerate(blocks, start=1):
        if old == "":
            return DeltaResult(False, reason=f"Block {index} has an empty OLD anchor.")
        occurrences = text.count(old)
        if occurrences == 0:
            return DeltaResult(
                False,
                reason=(
                    f"Block {index} OLD anchor is not present in the fetched file. "
                    "Re-read the exact current text for that candidate."
                ),
            )
        if occurrences > 1:
            return DeltaResult(
                False,
                reason=(
                    f"Block {index} OLD anchor occurs {occurrences} times and is ambiguous. "
                    "Extend the anchor until it is unique."
                ),
            )
        text = text.replace(old, new, 1)
        applied.append(old.splitlines()[0][:80] if old.splitlines() else "")

    ratio = changed_ratio(base, text)
    return DeltaResult(
        True, content=text, blocks=len(blocks), changed_ratio=ratio, applied=tuple(applied)
    )


def changed_ratio(before: str, after: str) -> float:
    """Fraction of lines the write touches, against the larger of the two."""
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    denominator = max(len(old_lines), len(new_lines))
    if denominator == 0:
        return 0.0
    changed = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed / denominator


# Absolute floor: a small honest edit is never judged by ratio alone, so a
# two-line fix in a three-line file is not refused for being "most of" it.
HARD_MIN_CHANGED_LINES = 20


def changed_lines(before: str, after: str) -> int:
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    total = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            total += max(i2 - i1, j2 - j1)
    return total


def write_within_budget(
    before: str, after: str, *, max_ratio: float = HARD_MAX_CHANGED_RATIO
) -> tuple[bool, str]:
    """
    Whether a write touches only the candidates it claims to change.

    A byte-identical write is refused as a no-op: it spends a route and an
    audit entry to change nothing. Beyond the absolute floor, a write that
    rewrites more than ``max_ratio`` of the file is refused as a bulk
    rewrite; the agent is told to send the changed candidates instead.
    """
    if before == after:
        return False, "This write is byte-identical to the fetched file; nothing changed."
    ratio = changed_ratio(before, after)
    touched = changed_lines(before, after)
    if touched <= HARD_MIN_CHANGED_LINES:
        return True, ""
    cap = min(float(max_ratio), HARD_MAX_CHANGED_RATIO)
    if ratio > cap:
        return False, (
            f"This write rewrites {ratio:.0%} of the file ({touched} lines), over the "
            f"{cap:.0%} bulk-rewrite cap. Send only the changed candidates as a "
            f"'{DELTA_SENTINEL}' payload."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Exact targets: a read returns the block asked for, not the file around it.
# ---------------------------------------------------------------------------

# Agent-supplied targeting fields. They are not part of the server schema, so
# the hook records them and strips them from the call before it executes.
TARGET_LINES_KEY = "tb_lines"
TARGET_ANCHOR_KEY = "tb_anchor"
TARGET_KEYS = (TARGET_LINES_KEY, TARGET_ANCHOR_KEY)

HARD_MAX_TARGET_LINES = 80
ANCHOR_CONTEXT_LINES = 8

_LINES_RE = re.compile(r"^\s*(\d+)\s*(?:-|:|\.\.)\s*(\d+)\s*$")


def parse_target_lines(value: object) -> tuple[int, int] | None:
    """``"120-160"`` -> (120, 160). A single number targets that one line."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return (value, value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        number = int(text)
        return (number, number) if number > 0 else None
    match = _LINES_RE.match(text)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < start:
        return None
    return (start, end)


@dataclass
class TargetStore:
    """What the agent said it was after, recorded before the call runs."""

    root: Path

    def _key(self, path: str, ref: str) -> Path:
        digest = hashlib.sha256(f"target\x00{ref}\x00{path}".encode()).hexdigest()[:32]
        return self.root / f"t_{digest}.json"

    def put(self, path: str, ref: str, target: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = {"path": path, "ref": ref, "at": int(time.time()), **target}
        destination = self._key(path, ref)
        tmp = destination.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(destination)

    def get(self, path: str, ref: str) -> dict[str, Any] | None:
        try:
            with self._key(path, ref).open(encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None


def slice_to_target(content: str, target: dict[str, Any] | None) -> tuple[str, str]:
    """
    The exact block, with real line numbers, or a reason it could not be cut.

    Line numbers are carried so the agent can navigate to a precise location
    instead of counting lines in an excerpt.
    """
    lines = content.splitlines()
    if not target:
        return "", "no target was recorded for this read"

    anchor = target.get(TARGET_ANCHOR_KEY)
    if isinstance(anchor, str) and anchor.strip():
        needle = anchor.strip()
        hits = [i for i, line in enumerate(lines, start=1) if needle in line]
        if not hits:
            return "", f"anchor {needle!r} is not present in the file"
        if len(hits) > 1:
            return "", f"anchor {needle!r} matches {len(hits)} lines; make it unique"
        centre = hits[0]
        start = max(1, centre - ANCHOR_CONTEXT_LINES)
        end = min(len(lines), centre + ANCHOR_CONTEXT_LINES)
    else:
        span = parse_target_lines(target.get(TARGET_LINES_KEY))
        if not span:
            return "", "no usable target range was recorded"
        start, end = span
        if start > len(lines):
            return "", f"line {start} is past the end of the file ({len(lines)} lines)"
        end = min(end, len(lines))
        if end - start + 1 > HARD_MAX_TARGET_LINES:
            end = start + HARD_MAX_TARGET_LINES - 1

    width = len(str(end))
    body = "\n".join(f"{number:>{width}}| {lines[number - 1]}" for number in range(start, end + 1))
    return body, ""
