#!/usr/bin/env python3
"""Universal PreToolUse safety, observation and boundary-self-protection guard."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(
    os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])
).resolve()
CONFIG_PATH = PROJECT_DIR / "config" / "observation_boundary.json"
POLICY_PATH = PROJECT_DIR / "config" / "command_policy.json"

HARD_MAX_READ_LINES = 80
DEFAULT_READ_TOOLS = {"Read"}
DEFAULT_MCP_READ_PATTERNS = [r"mcp__Desktop_Commander__read_file$"]

PROTECTED_BOUNDARY_PATHS = frozenset(
    {
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/_gh_contract.py",
        ".claude/hooks/pre_tool_use.py",
        ".claude/hooks/observation_gate.py",
        ".claude/hooks/observation_failure.py",
        ".claude/hooks/quality_gate.py",
        ".claude/hooks/completion_gate.py",
        ".claude/hooks/task_lifecycle.py",
        ".claude/hooks/precompact_checkpoint.py",
        ".claude/hooks/session-start.sh",
        "config/observation_boundary.json",
        "config/command_policy.json",
        "src/agent_control/reliability.py",
        "src/agent_control/completion.py",
        "tests/test_observation_boundary_contract.py",
        "tests/test_pre_tool_use_hook.py",
        "tests/test_github_route_contract.py",
        "CLAUDE.md",
        ".mcp.json",
        ".github/workflows",
        ".github/CODEOWNERS",
        "config/quality_registry.json",
        "common/command_schema.py",
        "common/shell_exec.py",
        "scripts/ci_diagnostics.py",
        "scripts/prfail",
        "scripts/apply_ci_patch.py",
        "scripts/ci_verdict.py",
        "scripts/local_checks.py",
        "tests/test_ci_diagnostics.py",
        "tests/test_ci_log_access.py",
        "tests/test_local_checks.py",
        "tests/test_apply_ci_patch.py",
        "tests/test_ci_failure_notify_workflow.py",
        "tests/test_ci_notice_supersession.py",
        "tests/test_merge_queue_wiring.py",
        "docs/quality/REQUIREMENTS_TRACEABILITY.md",
        "AGENTS.md",
    }
)
PROTECTED_MUTATION_TOOLS = {
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__github__create_or_update_file",
    "mcp__github__push_files",
    "mcp__github__delete_file",
}

BASH_MUTATOR_RE = re.compile(
    r"(?i)(?:>|>>|tee\b|sed\s+-i\b|perl\s+-pi\b|"
    r"python(?:3)?\s+(?:-c\b|\S+)|ruby\s+-e\b|"
    r"\b(?:cp|mv|rm|dd|git\s+(?:apply|checkout|restore|reset|clean))\b)"
)


# Hard, code-owned allowlist lives in _gh_contract.py beside this hook so that
# PreToolUse and PostToolUse enforce one inventory. Configuration and policy
# mode cannot add a route; they can only narrow one.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gh_contract import (  # noqa: E402
    CONTRACTS,
    DENIED_TOOLS,
    GITHUB_REPOSITORY,
    GITHUB_TOOL_PREFIX,
    HARD_MAX_NEW_FILE_LINES,
    HARD_MAX_PER_PAGE,
    TARGET_ANCHOR_KEY,
    TARGET_KEYS,
    TARGET_LINES_KEY,
    VERIFIED_SERVER_TOOLS,
    BlobStore,
    TargetStore,  # noqa: E402
    apply_delta,
    is_delta_payload,
    parse_target_lines,
    unresolved_tools,
    write_within_budget,
)

BLOB_ROOT = PROJECT_DIR / ".claude" / ".hook-state" / "gh_blobs"

GITHUB_COMMENT_URL_RE = re.compile(
    r"(?i)(?:api\.github\.com/repos/[^/]+/[^/]+/(?:issues/(?:comments/\d+|\d+/comments)|pulls/\d+/(?:comments|reviews))|api\.github\.com/graphql)"
)
GITHUB_CI_URL_RE = re.compile(
    r"(?i)(?:/actions(?:/|\?|$)|/check-runs?(?:/|\?|$)|"
    r"/check-suites?(?:/|\?|$)|/statuses?(?:/|\?|$)|"
    r"/commits/[^/?]+/(?:status|statuses)(?:/|\?|$)|/caches(?:/|\?|$))"
)
GITHUB_BRANCH_WRITE_TOOLS = frozenset(
    {
        "create_branch",
        "create_file",
        "update_file",
        "create_or_update_file",
        "push_files",
        "update_ref",
    }
)
GITHUB_BOUNDARY_TREE_TOOLS = frozenset(
    {"create_file", "update_file", "create_or_update_file", "push_files", "create_tree"}
)


def _hook_input(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("tool_input") or {}
    return value if isinstance(value, dict) else {}


def _owner_name(value: object) -> str:
    if isinstance(value, dict):
        for key in ("login", "name", "slug"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return value.strip() if isinstance(value, str) else ""


def _normalise_repo_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parts: list[str] = []
    for part in value.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _github_repo_refs(data: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    owner = _owner_name(data.get("owner") or data.get("org") or data.get("organization"))
    for key in (
        "repository_full_name",
        "repo_full_name",
        "repository_name",
        "repository",
        "repo",
        "repo_name",
        "full_name",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            nested_owner = _owner_name(value.get("owner") or value.get("org"))
            nested_repo = _owner_name(
                value.get("name") or value.get("repo") or value.get("full_name")
            )
            if nested_owner and nested_repo:
                refs.append(f"{nested_owner}/{nested_repo}".lower())
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        match = re.search(
            r"(?i)(?:api\.github\.com/repos/|github\.com/|raw\.githubusercontent\.com/)"
            r"([^/\s?#]+)/([^/\s?#]+)",
            value,
        )
        if match:
            refs.append(f"{match.group(1)}/{match.group(2)}".lower())
        elif "/" in value:
            pieces = value.strip("/").split("/")
            if len(pieces) >= 2:
                refs.append(f"{pieces[0]}/{pieces[1]}".lower())
        elif owner:
            refs.append(f"{owner}/{value}".lower())

    for key in ("url", "raw_url", "html_url", "repository_url"):
        value = data.get(key)
        if isinstance(value, str):
            match = re.search(
                r"(?i)(?:api\.github\.com/repos/|github\.com/|raw\.githubusercontent\.com/)"
                r"([^/\s?#]+)/([^/\s?#]+)",
                value,
            )
            if match:
                refs.append(f"{match.group(1)}/{match.group(2)}".lower())

    if isinstance(data.get("query"), str):
        for match in re.finditer(r"(?i)\brepo:([^\s]+)", data["query"]):
            refs.append(match.group(1).strip().rstrip(")").lower())
    return list(dict.fromkeys(refs))


def _github_boundary_write_violation(tool: str, data: dict[str, Any]) -> str:
    paths: list[str] = []
    for key in ("path", "file_path", "filename", "notebook_path"):
        if isinstance(data.get(key), str):
            paths.append(_normalise_repo_path(data[key]))
    if tool.endswith("__push_files") and isinstance(data.get("files"), list):
        paths.extend(
            _normalise_repo_path(item.get("path"))
            for item in data["files"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )
    if tool.endswith("__create_tree") and isinstance(data.get("tree_elements"), list):
        for item in data["tree_elements"]:
            if not isinstance(item, dict):
                continue
            path = _normalise_repo_path(item.get("path"))
            if path:
                paths.append(path)
            if item.get("sha") is None:
                return "Universal boundary: Git tree deletions are denied."
    protected = set(PROTECTED_BOUNDARY_PATHS)
    cfg = _load_observation_config()
    configured = cfg.get("protected_boundary_paths")
    if isinstance(configured, list):
        protected.update(str(item).replace("\\", "/").lstrip("/") for item in configured)
    matched = next(
        (
            path
            for path in paths
            if path in protected
            or path == ".claude"
            or path.startswith(".claude/")
            or path == "src/agent_control"
            or path.startswith("src/agent_control/")
            or path == ".github/workflows"
            or path.startswith(".github/workflows/")
            or path == ".mcp.json"
        ),
        None,
    )
    if matched and any(tool.endswith(f"__{name}") for name in GITHUB_BOUNDARY_TREE_TOOLS):
        return f"Protected observation-boundary file mutation denied: {matched}."
    return ""


def _is_exact_patch_apply_command(command: str) -> bool:
    """Allow only the reviewed patcher with a quoted heredoc and one final terminator."""
    lines = command.strip().splitlines()
    if len(lines) < 4 or lines[-1] != "TB_CI_PATCH" or lines.count("TB_CI_PATCH") != 1:
        return False
    header = re.fullmatch(
        r"python3 scripts/apply_ci_patch\.py --pr [1-9][0-9]* --head-sha [0-9a-f]{40} --patch-stdin <<'TB_CI_PATCH'",
        lines[0],
    )
    if not header:
        return False
    body = lines[1:-1]
    return any(line.startswith("diff --git a/") for line in body) and any(
        line.startswith("@@ ") for line in body
    )


def _github_route_violation(event: dict[str, Any]) -> str:
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    if not tool:
        return (
            "Universal GitHub-only boundary: tool identity is missing; access denied. "
            "Use an approved GitHub MCP tool and retry."
        )
    if not tool.startswith(GITHUB_TOOL_PREFIX):
        # Non-GitHub routes are limited to exact report retrieval and the
        # report-bound patcher; neither is a raw Actions-log access route.
        if tool == "bash":
            data = _hook_input(event)
            command = data.get("command", "")
            canonical_report = re.compile(r"^\s*\./scripts/prfail\s+--pr\s+[1-9][0-9]*\s*$")
            if (
                isinstance(command, str)
                and canonical_report.fullmatch(command.strip())
                and not is_ci_log_access(command)
                and not _local_check_violation(command, _load_policy())
            ):
                return ""
            if isinstance(command, str) and _is_exact_patch_apply_command(command):
                header = command.splitlines()[0]
                if not _local_check_violation(header, _load_policy()):
                    return ""
        return (
            "Universal GitHub-only boundary: denied. Use an approved GitHub MCP tool; "
            "local exceptions are the exact prfail report command and the "
            "report-bound patch applier."
        )
    tail = tool[len(GITHUB_TOOL_PREFIX) :]
    gap = unresolved_tools()
    if gap:
        return (
            "Universal boundary: the route contract is incomplete for "
            f"{sorted(gap)[:3]}; every verified tool must be contracted or denied."
        )
    if tail not in VERIFIED_SERVER_TOOLS:
        return (
            f"Universal GitHub-only boundary: '{tail}' is not in the verified server tool "
            "inventory and is denied as unverified. Re-verify the contract before any new "
            "tool name is used."
        )
    denial = DENIED_TOOLS.get(tail)
    if denial:
        return f"Universal boundary: {tail} is denied. {denial}"
    if tail not in CONTRACTS:
        return f"Universal boundary: {tail} has no argument contract; a contract gap fails closed."

    data = _hook_input(event)
    for key in ("url", "raw_url", "html_url", "repository_url"):
        url = data.get(key)
        if isinstance(url, str) and GITHUB_COMMENT_URL_RE.search(url):
            return (
                "Arbitrary PR-comment and review reads are denied. Use only the "
                "./scripts/prfail --pr <PR_NUMBER> report command."
            )
        if isinstance(url, str) and GITHUB_CI_URL_RE.search(url):
            return (
                "CI logs, jobs, artifacts, checks, caches and statuses are denied "
                "on every GitHub route."
            )
    contract_problem = _contract_violation(tail, CONTRACTS[tail], data)
    if contract_problem:
        return contract_problem

    refs = _github_repo_refs(data)
    expected = GITHUB_REPOSITORY.lower()
    if not refs:
        return (
            "Universal boundary: GitHub requests must explicitly identify "
            f"{GITHUB_REPOSITORY}; unscoped or ambiguous repository access is denied."
        )
    if any(ref != expected for ref in refs):
        return f"Universal boundary: GitHub access is restricted to {GITHUB_REPOSITORY}."

    if tail in GITHUB_BRANCH_WRITE_TOOLS:
        if tail == "create_branch":
            # Verified server parameters: branch, from_branch. The previous
            # names (branch_name, base_ref) do not exist here, so every
            # legitimate branch creation was denied for a missing name.
            branch_name = _owner_name(data.get("branch")).removeprefix("refs/heads/").lower()
            from_branch = _owner_name(data.get("from_branch")).removeprefix("refs/heads/").lower()
            if not branch_name or branch_name in {"main", "master"}:
                return (
                    "Universal boundary: branch creation requires a non-main feature-branch name."
                )
            if from_branch and from_branch != "main":
                return (
                    "Universal boundary: new Claude work branches must be based directly on main."
                )
        else:
            branch = data.get("branch") or data.get("branch_name") or data.get("target_branch")
            branch_name = _owner_name(branch).removeprefix("refs/heads/").lower()
            if not branch_name or branch_name in {"main", "master"}:
                return (
                    "Universal boundary: GitHub file/ref writes require an explicit "
                    "non-main feature branch."
                )
            if tail == "update_ref":
                if data.get("force") is True:
                    return "Universal boundary: force-updating Git refs is denied."
                if not _owner_name(data.get("expected_sha")):
                    return "Universal boundary: ref updates require an expected_sha lease."

    if tail == "create_pull_request":
        head = _owner_name(data.get("head") or data.get("head_branch"))
        head = head.rsplit(":", 1)[-1].removeprefix("refs/heads/").lower()
        base = (
            _owner_name(data.get("base") or data.get("base_branch"))
            .removeprefix("refs/heads/")
            .lower()
        )
        if not head or head in {"main", "master"}:
            return "Universal boundary: pull requests must use a feature branch as their head."
        if base != "main":
            return "Universal boundary: feature pull requests must target main."

    return _github_boundary_write_violation(tool, data)


def _contract_violation(tail: str, contract: Any, data: dict[str, Any]) -> str:
    """Enforce one tool's argument contract. Every refusal states its reason."""
    if contract.methods:
        method = str(data.get("method") or "").strip().lower()
        if not method:
            return f"Universal boundary: {tail} requires an explicit method."
        if method not in contract.methods:
            return (
                f"Universal boundary: {tail} method '{method}' is denied. Allowed: "
                f"{', '.join(sorted(contract.methods))}. CI run data, whole-PR reports and "
                "arbitrary comment bodies are not observation routes; use only "
                "./scripts/prfail --pr <PR_NUMBER> for finalized diagnostics."
            )
    for key in contract.required:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return f"Universal boundary: {tail} requires an explicit '{key}'."
    for key in contract.forbidden:
        if key in data:
            return f"Universal boundary: {tail} must not carry '{key}'."
    for key, denied in contract.denied_values:
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in denied:
            return (
                f"Universal boundary: {tail} {key}='{value}' is denied; it returns a whole "
                "report rather than the candidates needed."
            )
    if contract.bounded_page:
        per_page = data.get("perPage")
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            return (
                f"Universal boundary: {tail} requires an explicit integer perPage of at most "
                f"{HARD_MAX_PER_PAGE}; an unbounded result set is not a minimal-candidate route."
            )
        if per_page < 1 or per_page > HARD_MAX_PER_PAGE:
            return f"Universal boundary: {tail} perPage is bounded to 1..{HARD_MAX_PER_PAGE}."
    if contract.path_arg:
        raw = data.get(contract.path_arg)
        if not isinstance(raw, str) or not raw.strip():
            return (
                f"Universal boundary: {tail} requires an explicit repository '{contract.path_arg}'."
            )
        if raw.strip().endswith("/"):
            return (
                f"Universal boundary: {tail} must name exactly one file; a directory-wide "
                "read is not a minimal-candidate route."
            )
    return ""


def _github_read_plan(event: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """
    A read names the block it wants, and receives that block.

    A whole-repo result set or a whole file is not an answer to a question
    about a few lines, so the call must carry either an explicit line range
    (tb_lines) or a unique anchor (tb_anchor). Neither field exists on the
    server schema, so the target is recorded here and stripped from the call
    before it executes; PostToolUse cuts the response down to it.
    """
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    if tool != f"{GITHUB_TOOL_PREFIX}get_file_contents":
        return "", None

    data = _hook_input(event)
    path = str(data.get("path") or "").strip()
    ref = str(data.get("ref") or "").strip()
    anchor = data.get(TARGET_ANCHOR_KEY)
    lines = data.get(TARGET_LINES_KEY)

    has_anchor = isinstance(anchor, str) and anchor.strip()
    span = parse_target_lines(lines)
    if not has_anchor and span is None:
        return (
            "Universal boundary: a read must name its target. Pass "
            f"'{TARGET_LINES_KEY}' as \"start-end\" or '{TARGET_ANCHOR_KEY}' as a unique "
            "line of the block you need. Search first if you do not know it.",
            None,
        )
    if has_anchor and span is not None:
        return (
            f"Universal boundary: pass one of '{TARGET_LINES_KEY}' or "
            f"'{TARGET_ANCHOR_KEY}', not both.",
            None,
        )

    target: dict[str, Any] = (
        {TARGET_ANCHOR_KEY: anchor.strip()}
        if has_anchor
        else {TARGET_LINES_KEY: f"{span[0]}-{span[1]}"}
    )
    try:
        TargetStore(BLOB_ROOT).put(path, ref, target)
    except OSError as exc:
        return (
            f"Universal boundary: the read target could not be recorded ({exc.__class__.__name__}).",
            None,
        )

    stripped = {key: value for key, value in data.items() if key not in TARGET_KEYS}
    return "", stripped


def _github_write_plan(event: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """
    Reduce a file write to the candidates that changed.

    A delta payload is reconstructed against the body the agent was shown,
    which this hook kept on disk, so the model never has to hold a whole file
    to change a few lines of it. A literal write is measured against that same
    body and refused when it is a no-op or a bulk rewrite.
    """
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    if tool != f"{GITHUB_TOOL_PREFIX}create_or_update_file":
        return "", None

    data = _hook_input(event)
    path = str(data.get("path") or "").strip()
    content = data.get("content")
    if not isinstance(content, str):
        return "Universal boundary: create_or_update_file requires string content.", None

    store = BlobStore(BLOB_ROOT)
    record = store.get(path, str(data.get("branch") or "")) or store.any_ref(path)
    base = str(record.get("content") or "") if record else ""

    if is_delta_payload(content):
        if record is None:
            return (
                "Universal boundary: a delta write needs the fetched file. Call "
                f"get_file_contents for {path} at an explicit ref first.",
                None,
            )
        result = apply_delta(base, content)
        if not result.ok:
            return f"Universal boundary: delta write refused. {result.reason}", None
        allowed, reason = write_within_budget(base, result.content)
        if not allowed:
            return f"Universal boundary: {reason}", None
        updated = dict(data)
        updated["content"] = result.content
        if record.get("sha"):
            updated["sha"] = str(record["sha"])
        print(
            f"[pre_tool_use] delta write {path}: {result.blocks} block(s), "
            f"{result.changed_ratio:.0%} of file rewritten",
            file=sys.stderr,
        )
        return "", updated

    if record is None:
        if data.get("sha"):
            return (
                "Universal boundary: this write carries a blob sha for a file that was "
                "never fetched in this session; re-read the exact current text first.",
                None,
            )
        if len(content.splitlines()) > HARD_MAX_NEW_FILE_LINES:
            return (
                f"Universal boundary: a new file is bounded to {HARD_MAX_NEW_FILE_LINES} "
                "lines; split the change.",
                None,
            )
        print(f"[pre_tool_use] new file {path}", file=sys.stderr)
        return "", None

    allowed, reason = write_within_budget(base, content)
    if not allowed:
        return f"Universal boundary: {reason}", None
    if not data.get("sha") and record.get("sha"):
        updated = dict(data)
        updated["sha"] = str(record["sha"])
        return "", updated
    return "", None


# Any request whose subject is CI, logs, run records, reports or monitoring,
# whatever tool carries it. Direct GitHub observations remain blocked; the one
# authorized local route is the exact report-only ./scripts/prfail command.
CI_SUBJECT_RE = re.compile(
    r"(?i)(workflow|check_run|check-run|check_suite|check-suite|job_log|run_log|"
    r"\blogs?\b|artifact|annotation|combined_status|status_check|\bcheck[-_ ]?runs?\b|"
    r"rerun|re-run|monitor|watch|\bcaches?\b|actions/runs|/actions\b)"
)
CI_METHODS = frozenset({"get_status", "get_check_runs"})


def _ci_pr_hint(data: dict[str, Any]) -> int | None:
    for key in ("pullNumber", "pull_number", "pr_number", "issue_number", "number"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    for key in ("url", "html_url", "raw_url"):
        value = data.get(key)
        if isinstance(value, str):
            match = re.search(r"/(?:pulls?|issues)/(\d+)", value)
            if match:
                return int(match.group(1))
    return None


def _is_ci_request(tool: str, data: dict[str, Any]) -> bool:
    if tool.startswith(GITHUB_TOOL_PREFIX):
        tail = tool[len(GITHUB_TOOL_PREFIX) :]
        if CI_SUBJECT_RE.search(tail):
            return True
        method = str(data.get("method") or "").strip().lower()
        if method in CI_METHODS:
            return True
    for key in ("url", "html_url", "raw_url", "repository_url"):
        value = data.get(key)
        if isinstance(value, str) and GITHUB_CI_URL_RE.search(value):
            return True
    command = data.get("command")
    if isinstance(command, str) and command.strip():
        # The canonical report and report-bound patcher have exact allow rules.
        if re.fullmatch(
            r"\s*\./scripts/prfail\s+--pr\s+[1-9][0-9]*\s*", command
        ) or _is_exact_patch_apply_command(command):
            return False
        if is_ci_log_access(command) or CI_SUBJECT_RE.search(command):
            return True
    return False


def _ci_interception(event: dict[str, Any]) -> str | None:
    """
    Retain the fail-closed denial for direct CI connector observations.

    The exact local ./scripts/prfail --pr <N> command and the report-bound
    patcher are exempted in _is_ci_request and handled by the route gate; direct
    GitHub run/check/log observations never expose the underlying records.
    """
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    data = _hook_input(event)
    if not _is_ci_request(tool, data):
        return None
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    try:
        import ci_verdict
    except Exception:
        return "ci verdict unavailable: verdict module not importable"
    if not ci_verdict.has_credentials():
        # No credential, so no verdict can be established. Fall through to the
        # ordinary route denial rather than inventing an answer.
        return None
    try:
        return ci_verdict.verdict(_ci_pr_hint(data))
    except ci_verdict.VerdictError as exc:
        return f"ci verdict unavailable: {exc}"
    except Exception as exc:  # fail closed: never fall through to raw data
        return f"ci verdict unavailable: {type(exc).__name__}"


def _fail_closed(note: str) -> None:
    print(f"[pre_tool_use] fail-closed: {note}", file=sys.stderr)
    _emit(
        "deny",
        "Universal GitHub-only observation boundary could not verify this request; access denied.",
    )


sys.path.insert(0, str(PROJECT_DIR))

try:
    from common.command_schema import (
        CI_LOG_REFUSAL,
        LOCAL_CHECK_PATTERNS,
        classify,
        is_ci_log_access,
    )
except Exception:  # pragma: no cover
    CI_LOG_REFUSAL = (
        "THAPPAR — CI DIAGNOSTIC ACCESS BLOCKED. Use only ./scripts/prfail --pr <PR_NUMBER>."
    )
    classify = None  # type: ignore[assignment]

    LOCAL_CHECK_PATTERNS = (
        r"\bpytest\b",
        r"\bpython(?:3)?(?:\.\d+)?\s+-m\s+pytest\b",
        r"\b(?:python(?:3)?(?:\.\d+)?\s+-m\s+)?unittest\b",
        r"\b(?:tox|nox)\b",
        r"\\bcoverage(?:\\s+(?:run|combine|report|html|xml|erase))?\\b",
        r"\\bruff\\s+(?:check|format)\\b",
        r"\\b(?:mypy|pyright|flake8|pylint|black|isort)\\b",
        r"\\b(?:bandit|pip-audit|safety|semgrep|trivy)\\b",
        r"\\b(?:npx\\s+)?(?:vitest|jest|playwright|cypress)\\b",
        r"\\b(?:npm|pnpm|yarn)\\s+(?:test|run\\s+(?:test|build|lint|check)|build|lint|check)\\b",
        r"\\b(?:cargo\\s+(?:test|build|clippy)|go\\s+(?:test|build|vet))\\b",
        r"\\b(?:make|gmake)\\s+(?:test|build|lint|check|ci)\\b",
        r"\\b(?:cmake\\s+--build|ninja(?:\\s|$))",
        r"\\bpython(?:3)?(?:\\.\\d+)?\\s+-m\\s+build\\b",
        r"\\bpre-commit\\s+run\\b",
        r"\\b(?:actionlint|shellcheck)\\b",
        r"\\.claude/skills/quality-engineering/scripts/qe_gate\\.py\\b",
        r"\\bscripts/arch_gate\\.sh\\b",
        r"\\bpython(?:3)?(?:\\.\\d+)?\\s+(?:-m\\s+)?tests?/[^ \\t;&|]*\\.py\\b",
        r"\\bpython(?:3)?(?:\\.\\d+)?\\s+-m\\s+tests?(?:\\.|\\s)",
        r"\\bpython(?:3)?(?:\\.\\d+)?\\s+-m\\s+compileall\\b",
        r"\\bpython(?:3)?(?:\\.\\d+)?\\s+scripts/generate_(?:math|quality)_docs\\.py\\s+--check\\b",
        r"\\b(?:bash|sh)\\s+[^;&|]*(?:test|tests|testing|build|lint|check|quality[-_]gate|security[-_]scan)[^;&|]*\\.sh\\b",
        r"\\bnode\\s+[^;&|]*(?:test|tests|testing|build|lint|check)[^;&|]*\\.(?:js|mjs|cjs|ts)\\b",
    )

    def is_ci_log_access(command: str) -> bool:
        return False


def _emit(decision: str, reason: str = "", updated_input: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if decision == "allow" and updated_input is not None:
        payload["hookSpecificOutput"]["updatedInput"] = updated_input
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    raise SystemExit(0)


def _fail_open(note: str) -> None:
    print(f"[pre_tool_use] degraded, allowing: {note}", file=sys.stderr)
    _emit("allow")


def _load_policy() -> dict[str, Any]:
    try:
        with POLICY_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        print(
            "[pre_tool_use] policy unavailable; hard observation controls remain active",
            file=sys.stderr,
        )
        return {}


def _load_observation_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {}


def _enforcement(policy: dict[str, Any]) -> str:
    override = os.environ.get("TB_COMMAND_POLICY", "").strip().lower()
    if override in {"block", "warn"}:
        return override
    configured = str(policy.get("enforcement", "block")).lower()
    return configured if configured in {"block", "warn"} else "block"


def _local_check_violation(command: str, policy: dict[str, Any]) -> str:
    cfg = policy.get("local_checks", {})
    wrapper = str(cfg.get("wrapper", "scripts/local_checks.py"))
    if wrapper in command:
        return "Local check execution is disabled by repository policy; legacy local_checks.py commands are retired."
    patterns = set(LOCAL_CHECK_PATTERNS) | set(cfg.get("blocked_patterns", []))
    for pattern in patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return "Local test/build/check execution is disabled by repository policy; push the change and use GitHub Actions as the sole execution path."
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


def _read_boundary_violation(event: dict[str, Any], _policy: dict[str, Any]) -> str:
    cfg = _load_observation_config()
    tool = str(event.get("tool_name", ""))
    names = set(DEFAULT_READ_TOOLS)
    configured = cfg.get("native_read_tools")
    if isinstance(configured, list):
        names.update(str(item) for item in configured)

    patterns = list(DEFAULT_MCP_READ_PATTERNS)
    configured_patterns = cfg.get("mcp_read_tool_patterns")
    if isinstance(configured_patterns, list):
        patterns.extend(str(item) for item in configured_patterns)

    if tool not in names and not any(
        re.search(pattern, tool, re.IGNORECASE) for pattern in patterns
    ):
        return ""

    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "Read operations require an explicit bounded range."

    if tool == "NotebookRead":
        return ""

    limit = _read_limit(tool_input)
    configured_max = cfg.get("max_read_lines", HARD_MAX_READ_LINES)
    try:
        max_lines = min(int(configured_max), HARD_MAX_READ_LINES)
    except (TypeError, ValueError):
        max_lines = HARD_MAX_READ_LINES

    if limit is None:
        return (
            f"Read operations require an explicit bounded range of at most {max_lines} lines. "
            "Use a targeted search first, then request the smallest exact range needed."
        )
    if limit < 1 or limit > max_lines:
        return f"Read operations are permanently bounded to at most {max_lines} lines."
    return ""


def _protected_boundary_violation(event: dict[str, Any]) -> str:
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""

    targets: list[str] = []
    for key in ("file_path", "path", "filename", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            raw = value.replace("\\", "/")
            marker = str(PROJECT_DIR).replace("\\", "/").rstrip("/") + "/"
            if raw.startswith(marker):
                raw = raw[len(marker) :]
            if raw.startswith("./"):
                raw = raw[2:]
            targets.append(raw.lstrip("/"))

    protected = [path for path in targets if path in PROTECTED_BOUNDARY_PATHS]

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        if _is_exact_patch_apply_command(command):
            # The command necessarily names the protected patcher itself. Inspect
            # each diff target instead; the patcher independently enforces the
            # same denylist before applying anything.
            for line in command.strip().splitlines():
                match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
                if not match:
                    continue
                target = match.group(1)
                if target != match.group(2):
                    return "Report-bound patcher denied a diff that changes file paths."
                if target in PROTECTED_BOUNDARY_PATHS or target.startswith(
                    (".claude/", ".github/workflows/", "src/agent_control/")
                ):
                    return f"Protected observation-boundary file mutation denied: {target}."
        else:
            for protected_path in PROTECTED_BOUNDARY_PATHS:
                absolute_path = (PROJECT_DIR / protected_path).as_posix()
                if (
                    protected_path in command or absolute_path in command
                ) and BASH_MUTATOR_RE.search(command):
                    return f"Protected observation-boundary file mutation denied: {protected_path}."

    if not protected:
        return ""

    if tool.lower() in {name.lower() for name in PROTECTED_MUTATION_TOOLS}:
        return f"Protected observation-boundary file mutation denied: {protected[0]}."

    if (
        any(key in tool_input for key in ("content", "new_string", "replacement"))
        and tool != "Read"
    ):
        return f"Protected observation-boundary file mutation denied: {protected[0]}."

    return ""


def _violations(event: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    protected_violation = _protected_boundary_violation(event)
    if protected_violation:
        problems.append(protected_violation)

    read_violation = _read_boundary_violation(event, policy)
    if read_violation:
        problems.append(read_violation)

    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    exact_patch_apply = isinstance(command, str) and _is_exact_patch_apply_command(command)
    if (
        tool == "Bash"
        and isinstance(command, str)
        and not exact_patch_apply
        and is_ci_log_access(command)
    ):
        problems.append(CI_LOG_REFUSAL)

    if not isinstance(command, str) or not command.strip():
        return problems

    local_check = "" if exact_patch_apply else _local_check_violation(command, policy)
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
        classification_command = command.splitlines()[0] if exact_patch_apply else command
        if classify(classification_command) == "destructive" and marker not in command:
            problems.append(
                "Destructive command. Use the project's authorized destructive "
                "execution path, or prefix it with "
                f"{marker} after explicit human approval."
            )

    if not problems:
        try:
            from src.agent_control.hooks import git_guard

            guard = (
                git_guard(event)
                if tool == "Bash" and "git" in command and not exact_patch_apply
                else ""
            )
            if guard:
                problems.append(guard)
        except Exception as exc:  # pragma: no cover
            print(f"[pre_tool_use] git guard degraded, allowing: {exc!r}", file=sys.stderr)

    return problems


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _fail_closed("empty hook payload")
            return
        event = json.loads(raw)
        if not isinstance(event, dict):
            _fail_closed("hook payload is not an object")
            return
    except Exception:
        _fail_closed("malformed hook payload")
        return

    # Before any route decision: a CI request is answered with the verdict and
    # never with the record it came from.
    ci_answer: str | None = None
    try:
        ci_answer = _ci_interception(event)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[pre_tool_use] ci interception degraded: {exc!r}", file=sys.stderr)
    if ci_answer is not None:
        _emit("deny", ci_answer)

    try:
        route_problem = _github_route_violation(event)
        if route_problem:
            # Hard route denial ignores TB_COMMAND_POLICY, warning mode, and config flags.
            _emit("deny", route_problem)
        policy = _load_policy()
        problems = _violations(event, policy)
    except Exception as exc:
        _fail_closed(f"policy evaluation failed ({type(exc).__name__})")
        return

    if not problems:
        write_problem, updated_input = _github_write_plan(event)
        if write_problem:
            _emit("deny", write_problem)
        if updated_input is None:
            read_problem, updated_input = _github_read_plan(event)
            if read_problem:
                _emit("deny", read_problem)
        _emit("allow", updated_input=updated_input)

    reason = " ".join(dict.fromkeys(problems))
    hard = any(
        marker in reason
        for marker in (
            "Protected observation-boundary",
            "Read operations",
            CI_LOG_REFUSAL,
            "Local check execution is disabled by repository policy",
            "Local test/build/check execution is disabled by repository policy",
        )
    )
    if _enforcement(policy) == "warn" and not hard:
        print(f"[pre_tool_use] policy warning: {reason}", file=sys.stderr)
        _emit("allow")

    _emit("deny", reason)


if __name__ == "__main__":
    main()
