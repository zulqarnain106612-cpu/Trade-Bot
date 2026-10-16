#!/usr/bin/env python3
"""Apply a reviewer-generated unified diff locally, bound to the diagnosed PR head.

This utility validates the current finalized PR diagnostic report and head SHA,
then changes only the local working tree. It never stages, commits, pushes,
changes branches, or runs tests/quality gates.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_PATCH_BYTES = 2 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
PROTECTED_PATCH_PATHS = frozenset(
    {
        ".github/CODEOWNERS",
        ".mcp.json",
        "config/observation_boundary.json",
        "config/command_policy.json",
        "config/quality_registry.json",
        "common/command_schema.py",
        "common/shell_exec.py",
        "scripts/ci_diagnostics.py",
        "scripts/prfail",
        "scripts/apply_ci_patch.py",
        "scripts/ci_verdict.py",
        "scripts/local_checks.py",
        "tests/test_observation_boundary_contract.py",
        "tests/test_pre_tool_use_hook.py",
        "tests/test_github_route_contract.py",
        "tests/test_ci_diagnostics.py",
        "tests/test_ci_log_access.py",
        "tests/test_local_checks.py",
        "tests/test_ci_failure_notify_workflow.py",
        "tests/test_ci_notice_supersession.py",
        "tests/test_merge_queue_wiring.py",
        "tests/test_apply_ci_patch.py",
        "docs/quality/REQUIREMENTS_TRACEABILITY.md",
        "CLAUDE.md",
        "AGENTS.md",
    }
)
PROTECTED_PATCH_PREFIXES = (".claude/", ".github/workflows/", "src/agent_control/")


def is_protected_patch_path(relative: str) -> bool:
    """Control-plane files require a separately reviewed maintainer change."""
    normalized = relative.replace("\\", "/").lstrip("/")
    return normalized in PROTECTED_PATCH_PATHS or any(
        normalized.startswith(prefix) for prefix in PROTECTED_PATCH_PREFIXES
    )


class PatchError(RuntimeError):
    """A local patch cannot safely be applied to this checkout."""


def _git(*args: str, patch: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            input=patch,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchError(f"git command could not run ({type(exc).__name__})") from exc
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else "git command failed"
        raise PatchError(reason[:240])
    return result.stdout.strip()


def _current_head() -> str:
    sha = _git("rev-parse", "--verify", "HEAD")
    if not SHA_RE.fullmatch(sha):
        raise PatchError("local HEAD is not a recognized 40-character commit SHA")
    return sha


def _safe_existing_file(relative: str) -> Path:
    if not relative or "\\" in relative or "\x00" in relative:
        raise PatchError("patch contains an empty or unsafe path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise PatchError(f"patch path is not repository-relative: {relative!r}")
    if pure.parts[0] == ".git" or relative.startswith(".git/"):
        raise PatchError("patches targeting Git metadata are forbidden")
    candidate = ROOT.joinpath(*pure.parts)
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_root = ROOT.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PatchError(
            f"patch path escapes the repository or has a missing parent: {relative!r}"
        ) from exc

    # Reject symlink components even when they happen to resolve within ROOT:
    # the target could be replaced between review and patch application.
    current = ROOT
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise PatchError(f"patch path traverses a symbolic link: {relative!r}")
    if not candidate.exists() or not candidate.is_file():
        raise PatchError(f"patch must edit an existing regular file: {relative!r}")
    return candidate


def _validate_patch(patch: str) -> list[str]:
    if not patch.strip():
        raise PatchError("patch input is empty")
    encoded = patch.encode("utf-8")
    if len(encoded) > MAX_PATCH_BYTES:
        raise PatchError(f"patch exceeds the {MAX_PATCH_BYTES}-byte safety limit")
    if "\x00" in patch:
        raise PatchError("binary patch input is not supported")

    forbidden_line_prefixes = (
        "GIT binary patch",
        "Binary files ",
        "new file mode ",
        "deleted file mode ",
        "old mode ",
        "new mode ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
    )
    if any(line.startswith(forbidden_line_prefixes) for line in patch.splitlines()):
        raise PatchError(
            "only in-place text edits are accepted; add/delete/rename/mode/binary changes are refused"
        )

    paths: list[str] = []
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("diff --git "):
            index += 1
            continue
        match = DIFF_HEADER_RE.fullmatch(line)
        if not match:
            raise PatchError(
                "quoted or whitespace-containing patch paths are not supported; use simple repository-relative paths"
            )
        old_path, new_path = match.groups()
        if is_protected_patch_path(old_path):
            raise PatchError(
                f"patch targets protected control-plane path {old_path!r}; use a separately reviewed maintainer change"
            )
        if old_path != new_path:
            raise PatchError(
                "renames and path changes are forbidden; every patch section must edit one existing file in place"
            )
        if old_path in paths:
            raise PatchError(f"patch contains duplicate sections for {old_path!r}")
        _safe_existing_file(old_path)

        # Ensure the normal unified-diff source headers agree with the Git path.
        source_header = f"--- a/{old_path}"
        target_header = f"+++ b/{old_path}"
        try:
            old_header_index = lines.index(source_header, index + 1)
        except ValueError as exc:
            raise PatchError(f"patch is missing its source header for {old_path!r}") from exc
        try:
            new_header_index = lines.index(target_header, old_header_index + 1)
        except ValueError as exc:
            raise PatchError(f"patch is missing its target header for {old_path!r}") from exc
        next_section = next(
            (pos for pos in range(index + 1, len(lines)) if lines[pos].startswith("diff --git ")),
            len(lines),
        )
        if new_header_index >= next_section:
            raise PatchError(f"patch section headers do not match {old_path!r}")
        if not any(line.startswith("@@ ") for line in lines[new_header_index + 1 : next_section]):
            raise PatchError(f"patch section for {old_path!r} has no text-edit hunk")
        paths.append(old_path)
        index = next_section

    if not paths:
        raise PatchError("input is not a Git unified diff")
    if any(line.startswith(("diff --cc ", "diff --combined ", "Submodule ")) for line in lines):
        raise PatchError("combined and submodule patches are not supported")
    return paths


def _verify_diagnostic_report(pr_number: int, expected_head: str) -> None:
    """Require a finalized report for this PR/head before applying any diff."""
    cli = ROOT / "scripts" / "prfail"
    try:
        result = subprocess.run(
            [str(cli), "--pr", str(pr_number)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchError(
            f"the finalized diagnostic report could not be retrieved ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0:
        raise PatchError(
            "the finalized diagnostic report is unavailable for this PR head; refusing the patch"
        )
    try:
        records = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise PatchError("the diagnostic CLI returned malformed report data") from exc
    if not isinstance(records, list) or not records:
        raise PatchError(
            "the finalized report contains no diagnostic records; refusing an ungrounded patch"
        )
    if any(
        not isinstance(record, dict)
        or record.get("pr") != pr_number
        or record.get("head_sha") != expected_head
        for record in records
    ):
        raise PatchError("the diagnostic report does not match the requested PR and head SHA")


def apply_patch(pr_number: int, expected_head: str, patch: str) -> list[str]:
    if pr_number < 1:
        raise PatchError("PR number must be a positive decimal integer")
    if not SHA_RE.fullmatch(expected_head):
        raise PatchError("expected head must be a full 40-character lowercase commit SHA")

    paths = _validate_patch(patch)
    before = _current_head()
    if before != expected_head:
        raise PatchError(
            f"checkout HEAD {before} does not match diagnostic PR head {expected_head}; "
            "refusing to apply line-based edits against a different revision"
        )
    _verify_diagnostic_report(pr_number, expected_head)

    # Git validates every hunk against current file contents before it changes
    # anything. No fuzz, whitespace relaxation, 3-way fallback, or path escape.
    _git("apply", "--check", "--recount", patch=patch)
    _verify_diagnostic_report(pr_number, expected_head)
    if _current_head() != expected_head:
        raise PatchError("checkout HEAD changed during patch validation; patch was not applied")

    _git("apply", "--recount", patch=patch)
    if _current_head() != expected_head:
        raise PatchError("HEAD changed unexpectedly while applying the local patch")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int, help="diagnosed positive PR number")
    parser.add_argument(
        "--head-sha", required=True, help="full SHA returned by the finalized diagnostics report"
    )
    parser.add_argument(
        "--patch-stdin",
        action="store_true",
        required=True,
        help="read a unified diff from standard input; no patch path or arbitrary file write is accepted",
    )
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.buffer.read(MAX_PATCH_BYTES + 1)
        if len(raw) > MAX_PATCH_BYTES:
            raise PatchError(f"patch exceeds the {MAX_PATCH_BYTES}-byte safety limit")
        try:
            patch = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchError("patch input must be UTF-8 text") from exc
        paths = apply_patch(args.pr, args.head_sha, patch)
    except PatchError as exc:
        sys.stderr.write(f"apply_ci_patch: refused: {exc}\n")
        return 2

    sys.stdout.write(
        f"Applied local patch for PR #{args.pr} at {args.head_sha}; "
        f"changed {len(paths)} file(s): {', '.join(paths)}. "
        "Working tree only; nothing staged, committed, or pushed.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
