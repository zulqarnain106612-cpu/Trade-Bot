#!/usr/bin/env python3
"""Universal PreToolUse safety, observation and boundary-self-protection guard."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
        ".claude/hooks/pre_tool_use.py",
        ".claude/hooks/observation_gate.py",
        ".claude/hooks/observation_failure.py",
        "config/observation_boundary.json",
        "config/command_policy.json",
        "src/agent_control/reliability.py",
        "src/agent_control/completion.py",
        "tests/test_observation_boundary_contract.py",
        "tests/test_pre_tool_use_hook.py",
        "CLAUDE.md",
    }
)
PROTECTED_MUTATION_TOOLS = {
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__GitHub__create_file",
    "mcp__GitHub__update_file",
    "mcp__GitHub__delete_file",
}

BASH_MUTATOR_RE = re.compile(
    r"(?i)(?:>|>>|tee\b|sed\s+-i\b|perl\s+-pi\b|"
    r"python(?:3)?\s+(?:-c\b|\S+)|ruby\s+-e\b|"
    r"\b(?:cp|mv|rm|dd|git\s+(?:apply|checkout|restore|reset|clean))\b)"
)


# Hard, code-owned allowlist: configuration and policy mode cannot add routes.
GITHUB_REPOSITORY = "zulqarnain106612-cpu/Trade-Bot"
GITHUB_TOOL_PREFIX = "mcp__github__"
ALLOWED_GITHUB_TOOL_NAMES = frozenset(
    {
        # Exact tool names verified against the connected GitHub MCP server.
        # Unknown or future tool names remain denied unless deliberately reviewed here.
        "search",
        "search_issues",
        "search_prs",
        "search_commits",
        "search_branches",
        "fetch",
        "fetch_file",
        "fetch_blob",
        "fetch_commit",
        "fetch_pr_file_patch",
        "get_pr_diff",
        "get_pr_info",
        "list_pr_changed_filenames",
        "create_branch",
        "create_blob",
        "create_tree",
        "create_commit",
        "update_ref",
        "create_file",
        "update_file",
        "create_pull_request",
        "update_pull_request",
        "add_comment_to_issue",
        "add_review_to_pr",
    }
)
GITHUB_ALWAYS_DENIED_MARKERS = (
    "workflow",
    "artifact",
    "check_run",
    "check_suite",
    "combined_status",
    "status_check",
    "job_log",
    "run_log",
    "logs",
    "rerun",
    "cancel",
    "cache",
    "status",
    "watch",
    "subscribe",
    "dispatch",
    "monitor",
    "download",
    "delete",
    "merge",
)
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


def _github_fetch_endpoint_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False

    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        return False

    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/") if parsed.path.strip("/") else []
    if any(part in {".", ".."} for part in parts):
        return False

    if host == "api.github.com":
        if len(parts) < 3 or parts[0].lower() != "repos":
            return False
        suffix = [part.lower() for part in parts[3:]]
        if not suffix:
            return True
        if suffix[0] in {"contents", "pulls", "issues", "commits", "branches"}:
            return True
        if suffix == ["readme"]:
            return True
        if (
            len(suffix) >= 2
            and suffix[0] == "git"
            and suffix[1] in {"refs", "commits", "trees", "blobs"}
        ):
            return True
        return suffix[0] == "compare"

    if host == "github.com":
        return len(parts) >= 4 and parts[2].lower() in {"blob", "tree", "commit"}

    if host == "raw.githubusercontent.com":
        return len(parts) >= 4 and all(parts[:3])

    return False


def _github_route_violation(event: dict[str, Any]) -> str:
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    if not tool:
        return (
            "Universal GitHub-only boundary: tool identity is missing; access denied. "
            "Use an approved GitHub MCP tool and retry."
        )
    if not tool.startswith(GITHUB_TOOL_PREFIX):
        # The one non-GitHub route is an exact read-only subscription to the
        # current-head CI notice. Every other local or external tool stays denied.
        if tool == "bash":
            data = _hook_input(event)
            command = data.get("command", "")
            prepare = re.compile(
                r"^\s*(?:cd\s+[^;&|]+\s*&&\s*)?(?:python|python3|\.venv/bin/python|\.venv/bin/python3)\s+scripts/local_checks\.py\s+prepare\s*$",
                re.IGNORECASE,
            )
            if (
                isinstance(command, str)
                and prepare.fullmatch(command.strip())
                and not is_ci_log_access(command)
                and not _local_check_violation(command, _load_policy())
            ):
                return ""
        return (
            "Universal GitHub-only boundary: denied. The only local command exception "
            "is the exact SHA-filtered scripts/local_checks.py prepare subscription."
        )
    tail = tool[len(GITHUB_TOOL_PREFIX) :]
    if tail not in ALLOWED_GITHUB_TOOL_NAMES:
        return (
            f"Universal GitHub-only boundary: GitHub tool '{tail}' is not "
            "allowlisted and is denied."
        )
    if any(marker in tail for marker in GITHUB_ALWAYS_DENIED_MARKERS):
        return (
            "Universal boundary: CI run data and destructive GitHub operations are denied "
            f"({tail})."
        )

    data = _hook_input(event)
    for key in ("url", "raw_url", "html_url", "repository_url"):
        url = data.get(key)
        if isinstance(url, str) and GITHUB_COMMENT_URL_RE.search(url):
            return (
                "Arbitrary PR-comment and review reads are denied. Use only the "
                "SHA-filtered scripts/local_checks.py prepare subscription."
            )
        if isinstance(url, str) and GITHUB_CI_URL_RE.search(url):
            return (
                "CI logs, jobs, artifacts, checks, caches and statuses are denied "
                "on every GitHub route."
            )
    if tail == "fetch":
        url = data.get("url")
        if not isinstance(url, str):
            return "Universal boundary: GitHub fetch requires an explicit repository URL."
        if not _github_fetch_endpoint_allowed(url):
            return "Universal boundary: this generic GitHub endpoint is not allowlisted."

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
            branch_name = _owner_name(data.get("branch_name")).removeprefix("refs/heads/").lower()
            base_ref = _owner_name(data.get("base_ref")).removeprefix("refs/heads/").lower()
            if not branch_name or branch_name in {"main", "master"}:
                return (
                    "Universal boundary: branch creation requires a non-main feature-branch name."
                )
            if base_ref != "main" or data.get("sha"):
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
        "CI run data is not an allowed model-observation channel; use the PR notice comment."
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
        parts = [part.strip() for part in re.split(r"&&|;|\|\|", command) if part.strip()]
        allowed = (
            len(parts) in {1, 2}
            and all(part.startswith("cd ") for part in parts[:-1])
            and re.fullmatch(
                r"(?:python|python3|\.venv/bin/python|\.venv/bin/python3)\s+scripts/local_checks\.py\s+prepare",
                parts[-1],
            ) is not None
        )
        if not allowed:
            return "Local check execution is disabled by repository policy; only the exact local_checks.py prepare operation may read the current SHA-bound PR notice."
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
        for protected_path in PROTECTED_BOUNDARY_PATHS:
            absolute_path = (PROJECT_DIR / protected_path).as_posix()
            if (protected_path in command or absolute_path in command) and BASH_MUTATOR_RE.search(
                command
            ):
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

    if tool == "Bash" and isinstance(command, str) and is_ci_log_access(command):
        problems.append(CI_LOG_REFUSAL)

    if not isinstance(command, str) or not command.strip():
        return problems

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

    if not problems:
        try:
            from src.agent_control.hooks import git_guard

            guard = git_guard(event) if tool == "Bash" and "git" in command else ""
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
        _emit("allow")

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
