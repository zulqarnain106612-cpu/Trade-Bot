"""
Read-only Git state -- the facts every agent-control decision is drawn from.

Nothing in this module mutates a repository. Every call runs ``git`` with an
argv list (never a shell) and a timeout, under ``GIT_OPTIONAL_LOCKS=0`` -- so
a status taken by a hook can never race the agent's own ``git commit`` for
``index.lock`` -- and ``LC_ALL=C`` so the output parsed here is not
localised. Status is read with ``-z`` so a path containing spaces, quotes or
non-ASCII bytes is a path, not a parsing hazard.

Registry: GOV-061 (config/quality_registry.json).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.agent_control.model import GitSnapshot, StatusEntry, WorktreeState, utc_now

GIT_TIMEOUT_S = 15.0

# git-path name -> the operation a leftover file there means is in progress.
ACTIVE_OPERATION_PATHS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)


_STATUS_ARGS = ("status", "--porcelain=v2", "-z", "--untracked-files=all")


class GitError(RuntimeError):
    """A git command failed, timed out, or produced output that does not parse."""


@dataclass(frozen=True, slots=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GitLike(Protocol):
    cwd: Path

    def run(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | str | None = None,
        input_text: str | None = None,
    ) -> GitResult: ...


class GitRunner:
    """The one place a ``git`` process is started."""

    def __init__(self, cwd: Path | str, *, timeout_s: float = GIT_TIMEOUT_S) -> None:
        self.cwd = Path(cwd)
        self.timeout_s = timeout_s

    def run(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | str | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        env = dict(os.environ)
        env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
        command = " ".join(args)
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.cwd),
                input=input_text,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {command}: {exc}") from exc
        result = GitResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            detail = proc.stderr.strip()[:300]
            raise GitError(f"git {command} exited {proc.returncode}: {detail}")
        return result


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    parents: tuple[str, ...]
    subject: str


@dataclass(frozen=True, slots=True)
class BranchFields:
    head_sha: str | None
    branch: str | None
    upstream: str | None
    ahead: int | None
    behind: int | None


def parse_status_v2(text: str) -> tuple[dict[str, str], list[StatusEntry]]:
    """Parse ``git status --porcelain=v2 --branch -z`` into headers and entries."""
    headers: dict[str, str] = {}
    entries: list[StatusEntry] = []
    records = text.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            headers[key] = value
        elif record.startswith("1 "):
            parts = record.split(" ", 8)
            entries.append(StatusEntry(kind="changed", xy=parts[1], path=parts[8]))
        elif record.startswith("2 "):
            parts = record.split(" ", 9)
            if index >= len(records) or not records[index]:
                raise GitError(f"rename record without its source path: {record[:60]!r}")
            orig = records[index]
            index += 1
            entries.append(StatusEntry(kind="renamed", xy=parts[1], path=parts[9], orig_path=orig))
        elif record.startswith("u "):
            parts = record.split(" ", 10)
            entries.append(StatusEntry(kind="unmerged", xy=parts[1], path=parts[10]))
        elif record.startswith("? "):
            entries.append(StatusEntry(kind="untracked", xy="??", path=record[2:]))
        elif record.startswith("! "):
            entries.append(StatusEntry(kind="ignored", xy="!!", path=record[2:]))
        else:
            raise GitError(f"unrecognised porcelain v2 record: {record[:60]!r}")
    return headers, entries


def branch_fields(headers: dict[str, str]) -> BranchFields:
    oid = headers.get("branch.oid")
    head = headers.get("branch.head")
    ahead: int | None = None
    behind: int | None = None
    ab = headers.get("branch.ab")
    if ab:
        plus, minus = ab.split()
        ahead, behind = int(plus), -int(minus)
    return BranchFields(
        head_sha=None if oid in (None, "(initial)") else oid,
        branch=None if head in (None, "(detached)") else head,
        upstream=headers.get("branch.upstream"),
        ahead=ahead,
        behind=behind,
    )


def parse_worktrees(text: str) -> list[WorktreeState]:
    """Parse ``git worktree list --porcelain``. Unknown attribute lines are ignored."""
    trees: list[WorktreeState] = []
    current: WorktreeState | None = None
    for line in text.splitlines():
        if not line:
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = WorktreeState(path=value, head_sha=None, branch=None)
            trees.append(current)
        elif current is None:
            raise GitError(f"worktree attribute before any 'worktree' line: {line!r}")
        elif key == "HEAD":
            current.head_sha = value
        elif key == "branch":
            current.branch = value.removeprefix("refs/heads/")
        elif key == "detached":
            current.detached = True
        elif key == "bare":
            current.bare = True
        elif key == "locked":
            current.locked = True
        elif key == "prunable":
            current.prunable = True
    return trees


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def active_operations(runner: GitLike, cwd: Path | str | None = None) -> list[str]:
    """Merge / rebase / cherry-pick / revert / bisect operations left in progress."""
    args: list[str] = []
    for name, _ in ACTIVE_OPERATION_PATHS:
        args.extend(("--git-path", name))
    paths = runner.run("rev-parse", *args, cwd=cwd).stdout.splitlines()
    base = runner.cwd if cwd is None else Path(cwd)
    if len(paths) != len(ACTIVE_OPERATION_PATHS):
        raise GitError(f"rev-parse --git-path returned {len(paths)} paths")
    found: list[str] = []
    for (_, label), rel in zip(ACTIVE_OPERATION_PATHS, paths, strict=True):
        if _resolve(base, rel).exists() and label not in found:
            found.append(label)
    return found


def _dirty(entries: list[StatusEntry]) -> list[str]:
    return [entry.path for entry in entries if entry.kind != "ignored"]


def collect_snapshot(runner: GitLike, *, inspect_worktrees: bool = True) -> GitSnapshot:
    """Everything the agent-control layer needs to know about the repository, once."""
    located = runner.run("rev-parse", "--show-toplevel", "--git-common-dir").stdout.splitlines()
    if len(located) < 2:
        raise GitError("rev-parse did not report a top level and a common dir")
    root = Path(located[0])
    common = _resolve(runner.cwd, located[1]).resolve()
    status = runner.run(*_STATUS_ARGS, "--branch")
    headers, entries = parse_status_v2(status.stdout)
    fields = branch_fields(headers)
    worktrees = parse_worktrees(runner.run("worktree", "list", "--porcelain").stdout)
    if inspect_worktrees:
        for tree in worktrees:
            if tree.bare or tree.prunable:
                continue
            if Path(tree.path) == root:
                tree.dirty_paths = _dirty(entries)
                continue
            other = runner.run(*_STATUS_ARGS, check=False, cwd=tree.path)
            if other.returncode == 0:
                tree.dirty_paths = _dirty(parse_status_v2(other.stdout)[1])
    return GitSnapshot(
        repository_root=str(root),
        git_common_dir=str(common),
        branch=fields.branch,
        head_sha=fields.head_sha,
        upstream=fields.upstream,
        ahead=fields.ahead,
        behind=fields.behind,
        entries=entries,
        worktrees=worktrees,
        active_operations=active_operations(runner),
        captured_at=utc_now(),
    )


def commit_info(runner: GitLike, rev: str = "HEAD") -> CommitInfo:
    out = runner.run("log", "-1", "--format=%H%x00%P%x00%s", rev).stdout.rstrip("\n")
    parts = out.split("\0")
    if len(parts) != 3:
        raise GitError(f"could not read commit {rev!r}")
    sha, parents, subject = parts
    return CommitInfo(sha=sha, parents=tuple(parents.split()), subject=subject)


def commit_paths(runner: GitLike, info: CommitInfo) -> list[str]:
    """Paths the commit changed against its first parent (or everything, for a root)."""
    if info.parents:
        out = runner.run("diff", "--name-only", "-z", info.parents[0], info.sha).stdout
    else:
        out = runner.run(
            "diff-tree", "-r", "--name-only", "-z", "--no-commit-id", "--root", info.sha
        ).stdout
    return sorted(path for path in out.split("\0") if path)


def range_paths(runner: GitLike, base: str, head: str) -> list[str]:
    out = runner.run("diff", "--name-only", "-z", base, head).stdout
    return sorted(path for path in out.split("\0") if path)


def is_ancestor(runner: GitLike, ancestor: str, descendant: str) -> bool:
    result = runner.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitError(f"merge-base --is-ancestor failed: {result.stderr.strip()[:200]}")


def ref_sha(runner: GitLike, ref: str) -> str | None:
    result = runner.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def remote_head(runner: GitLike, remote: str, branch: str) -> str | None:
    """The branch head on the remote itself (network), or None if it does not exist."""
    out = runner.run("ls-remote", remote, f"refs/heads/{branch}").stdout.split()
    return out[0] if out else None
