#!/usr/bin/env python3
"""Collect SHA-bound non-success CI diagnostics in GitHub Actions.

Only this collector reads Actions job logs. The operator-facing CLI reads the
filtered Check Run report and has no Actions API fallback.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com/"
API_VERSION = "2022-11-28"
MASTER_NAME = "CI Non-Success Diagnostic Report"
PAGE_PREFIX = "CI Non-Success Diagnostic Report Page "
MASTER_EXTERNAL_PREFIX = "tradebot:ci-diagnostics:pr:"
PAGE_EXTERNAL_PREFIX = "tradebot:ci-diagnostics-page:"
REPORT_PAGE_BYTES = 45_000
MESSAGE_FRAGMENT_CHARS = 12_000
MAX_LOG_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_LOG_BYTES = 1024 * 1024 * 1024
COLLECT_DEADLINE_SECONDS = 22 * 60
POLL_INTERVAL_SECONDS = 10
LOG_FAILURES = frozenset({"failure", "timed_out", "cancelled", "canceled", "neutral", "action_required", "stale"})
_COLLECTOR_CONTEXT = False
ANNOTATION_LEVELS = frozenset({"failure", "error", "warning"})
WORKFLOW_INVENTORY: tuple[dict[str, Any], ...] = (
    {"name": "CI", "base_branches": None},
    {"name": "CodeQL", "base_branches": ("main",)},
    {"name": "Security", "base_branches": None},
    {"name": "Workflow lint", "base_branches": None},
)

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")
GHA_COMMAND_RE = re.compile(r"^::(error|warning)\s*(.*?)::(.*)$", re.IGNORECASE)
GHA_PROPERTY_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)=([^,]+)")
PY_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:, in .*)?$')
COMPILER_RE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\s][^:\n]*?\.[A-Za-z0-9_-]+):"
    r"(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<level>fatal error|error|warning)(?:\[[^\]]+\])?:\s*(?P<message>\S.*)$",
    re.IGNORECASE,
)
PAREN_COMPILER_RE = re.compile(
    r"^(?P<path>[^()\s][^()\n]*?\.[A-Za-z0-9_-]+)"
    r"\((?P<line>\d+)(?:,(?P<column>\d+))?\):\s*"
    r"(?P<level>fatal error|error|warning)(?:\s+[A-Z]+\d+)?[: ]\s*"
    r"(?P<message>\S.*)$",
    re.IGNORECASE,
)
PYTEST_RESULT_RE = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<node>\S+)(?:\s+-\s*(?P<message>.*))?$")
PYTEST_ERROR_RE = re.compile(r"^\s*E\s{2,}(?P<message>\S.*)$")
GENERIC_ERROR_RE = re.compile(r"^\s*(?P<kind>Error|Fatal error|FATAL|ERROR|FAILURE)(?:\s*[:\-]\s*|\s+)(?P<message>\S.*)$", re.IGNORECASE)
EXCEPTION_RE = re.compile(r"^\s*(?P<kind>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*(?P<message>\S.*)$")
PYTEST_SOURCE_RE = re.compile(
    r"^(?P<path>(?:tests|src|common|kg|rag_mongo|orchestrator|tools|review)/[^\s:]+\.py):"
    r"(?P<line>\d+)(?::(?P<column>\d+))?\b"
)
NOISE_RE = re.compile(
    r"^(?:Process completed with exit code \d+\.?|Error: Process completed with exit code \d+\.?|"
    r"The operation was canceled\.?|The job was canceled\.?|"
    r"No files were found with the provided path:.*|Canceling since a higher priority waiting request.*)$",
    re.IGNORECASE,
)


class DiagnosticError(RuntimeError):
    """The report cannot be safely finalized or retrieved."""


def _token() -> str:
    # The collector runs inside GitHub Actions and uses that job's scoped token.
    if _COLLECTOR_CONTEXT:
        value = os.environ.get("GITHUB_TOKEN", "").strip()
        if not value:
            raise DiagnosticError("GitHub Actions collector token unavailable")
        return value

    # Retrieval uses the existing gh CLI login. No extra token, environment
    # variable, credential creation, or secret setup is part of a diagnostic
    # cycle. The token is captured in-process and never printed to the agent.
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticError("existing GitHub CLI authentication is unavailable") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise DiagnosticError("existing GitHub CLI authentication is unavailable; run the approved login flow")
    return value


def _request(url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
             timeout: int = 30, allow_404: bool = False) -> tuple[bytes, int, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + _token(),
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "Trade-Bot-CI-diagnostics",
    }
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            # Never forward an API bearer token to a redirected host.
            return None

    opener = urllib.request.build_opener(NoRedirect)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read(), response.status, response.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return b"", 404, exc.headers
            if method.upper() in {"GET", "HEAD"} and exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                try:
                    delay = min(max(int(exc.headers.get("Retry-After", "")), 1), 20)
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(delay)
                last_error = exc
                continue
            raise DiagnosticError(
                f"GitHub API request failed ({exc.code}) for {urllib.parse.urlsplit(url).path}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if method.upper() in {"GET", "HEAD"} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DiagnosticError(
                f"GitHub API request unavailable for {urllib.parse.urlsplit(url).path}"
            ) from exc
    raise DiagnosticError(f"GitHub API unavailable for {urllib.parse.urlsplit(url).path}") from last_error


def _api_url(path: str) -> str:
    return API_ROOT + path.lstrip("/")


def _json(path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
          allow_404: bool = False) -> Any:
    payload, status, _ = _request(_api_url(path), method=method, body=body, allow_404=allow_404)
    if status == 404 and allow_404:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticError(f"GitHub API returned invalid JSON for {path}") from exc


def _pages(path: str, key: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for page in range(1, 51):
        separator = "&" if "?" in path else "?"
        payload = _json(f"{path}{separator}{urllib.parse.urlencode({'per_page': 100, 'page': page})}")
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise DiagnosticError(f"GitHub API response is missing the expected {key} collection")
        batch = payload[key]
        values.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return values
    raise DiagnosticError(f"GitHub API pagination safety limit reached while reading {key}")


def _repo_slug() -> str:
    configured = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if configured and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", configured):
        return configured
    root = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=root,
            capture_output=True, text=True, check=False, timeout=8,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticError("Cannot determine the GitHub repository") from exc
    remote = proc.stdout.strip().removesuffix(".git")
    if proc.returncode != 0:
        raise DiagnosticError("Cannot determine the GitHub repository")
    if remote.startswith("git@github.com:"):
        slug = remote.split(":", 1)[1]
    elif "github.com/" in remote:
        slug = remote.split("github.com/", 1)[1].split("?", 1)[0]
    else:
        raise DiagnosticError("origin is not a GitHub repository")
    slug = slug.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise DiagnosticError("origin is not a recognized GitHub repository")
    return slug


def _normal_conclusion(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_key(record: dict[str, Any]) -> str:
    fields = (
        "repository", "pr", "head_sha", "workflow_name", "workflow_run_id",
        "run_attempt", "job_id", "job_name", "step_name", "outcome_level",
        "conclusion", "category", "path", "start_line", "start_column",
        "end_line", "end_column", "message",
    )
    return _canonical_json({key: record.get(key) for key in fields})


def _source_priority(source: str) -> int:
    return {
        "GitHub check annotation API": 0, "GitHub Actions workflow-run API": 1,
        "GitHub Actions job API": 2, "GitHub Actions step API": 3,
        "GitHub Actions job log": 4,
    }.get(source, 9)


def _redact_message(value: Any) -> Any:
    """Mask common credentials before a diagnostic is included in a report."""
    if not isinstance(value, str):
        return value
    result = value
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "PRFAIL_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            result = result.replace(secret, "[REDACTED:" + name + "]")
    result = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b", "[REDACTED:GITHUB_TOKEN]", result)
    result = re.sub(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b", "[REDACTED:API_KEY]", result)
    result = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED:AWS_ACCESS_KEY_ID]", result)
    result = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}", "Bearer [REDACTED:AUTH_TOKEN]", result)
    result = re.sub(r"(?i)(https?://[^:@/\s]+:)[^@/\s]+(@)", r"\1[REDACTED:URL_PASSWORD]\2", result)
    result = re.sub(
        r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|token|credential|password|passwd|secret|private[_-]?key)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED:SECRET]", result,
    )
    result = re.sub(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED:PRIVATE_KEY]", result)
    return result


def _add_record(records: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    # Preserve structured identities and source paths byte-for-byte; redact only
    # the original free-text diagnostic message before report serialization.
    record["message"] = _redact_message(record.get("message"))
    key = _record_key(record)
    existing = records.get(key)
    if existing is None or _source_priority(str(record.get("source", ""))) < _source_priority(str(existing.get("source", ""))):
        records[key] = record


def _base_record(repository: str, pr: int, sha: str, workflow_name: str,
                 run: dict[str, Any], *, job: dict[str, Any] | None = None,
                 step: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "repository": repository, "pr": pr, "head_sha": sha, "workflow_name": workflow_name,
        "workflow_run_id": int(run["id"]), "run_attempt": int(run.get("run_attempt") or 1),
        "job_id": int(job["id"]) if job and isinstance(job.get("id"), int) else None,
        "job_name": str(job.get("name") or "") if job else None,
        "step_name": str(step.get("name") or "") if step else None,
        "outcome_level": "step" if step else ("job" if job else "workflow"),
        "conclusion": None, "category": None, "path": None, "start_line": None,
        "start_column": None, "end_line": None, "end_column": None,
        "message": None, "source": "",
    }


def _unquote_command_value(value: str) -> str:
    for marker, replacement in (("%25", "%"), ("%0D", "\r"), ("%0A", "\n"), ("%3A", ":"), ("%2C", ",")):
        value = value.replace(marker, replacement)
    return value


def _annotation_from_log(line: str) -> dict[str, Any] | None:
    match = GHA_COMMAND_RE.match(line)
    if not match:
        return None
    level, properties, message = match.groups()
    props = {key.lower(): _unquote_command_value(value) for key, value in GHA_PROPERTY_RE.findall(properties)}

    def integer(key: str) -> int | None:
        try:
            value = int(props[key])
            return value if value > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    return {
        "level": level.lower(), "path": props.get("file"),
        "start_line": integer("line"), "start_column": integer("col"),
        "end_line": integer("endline"), "end_column": integer("endcolumn"),
        "message": _unquote_command_value(message),
    }


def parse_log_lines(raw: str) -> list[dict[str, Any]]:
    """Extract explicit diagnostic formats only; ordinary log lines are discarded."""
    found: list[dict[str, Any]] = []
    last_python_frame: tuple[str, int] | None = None
    last_pytest_source: tuple[str, int | None, int | None] | None = None
    for original in raw.splitlines():
        line = TIMESTAMP_RE.sub("", original, count=1)
        if not line.strip() or NOISE_RE.fullmatch(line.strip()):
            continue
        annotation = _annotation_from_log(line)
        if annotation:
            if annotation["level"] in ANNOTATION_LEVELS:
                found.append({
                    "category": "check_annotation",
                    "annotation_level": "failure" if annotation["level"] == "error" else annotation["level"],
                    "path": annotation["path"], "start_line": annotation["start_line"],
                    "start_column": annotation["start_column"], "end_line": annotation["end_line"],
                    "end_column": annotation["end_column"], "message": annotation["message"],
                    "source": "GitHub Actions job log",
                })
            continue
        if line.startswith("##[error]"):
            message = line[len("##[error]"):].strip()
            if message and not NOISE_RE.fullmatch(message):
                found.append({
                    "category": "workflow_error_annotation", "path": None, "start_line": None,
                    "start_column": None, "end_line": None, "end_column": None,
                    "message": message, "source": "GitHub Actions job log",
                })
            continue
        frame = PY_FRAME_RE.match(line)
        if frame:
            try:
                last_python_frame = (frame.group(1), int(frame.group(2)))
            except ValueError:
                last_python_frame = None
            continue
        source_match = PYTEST_SOURCE_RE.match(line.strip())
        if source_match:
            try:
                last_pytest_source = (
                    source_match.group("path"), int(source_match.group("line")),
                    int(source_match.group("column")) if source_match.group("column") else None,
                )
            except ValueError:
                last_pytest_source = None
        compiler = COMPILER_RE.match(line.strip()) or PAREN_COMPILER_RE.match(line.strip())
        if compiler:
            found.append({
                "category": "compiler_" + compiler.group("level").lower().replace(" ", "_"),
                "path": compiler.group("path"), "start_line": int(compiler.group("line")),
                "start_column": int(compiler.group("column")) if compiler.group("column") else None,
                "end_line": None, "end_column": None, "message": line,
                "source": "GitHub Actions job log",
            })
            continue
        pytest_result = PYTEST_RESULT_RE.match(line.strip())
        if pytest_result:
            node = pytest_result.group("node")
            found.append({
                "category": "pytest_" + pytest_result.group("kind").lower(),
                "path": node.split("::", 1)[0] if node.startswith(("tests/", "src/")) else None,
                "start_line": None, "start_column": None, "end_line": None, "end_column": None,
                "message": line, "source": "GitHub Actions job log",
            })
            continue
        pytest_error = PYTEST_ERROR_RE.match(line)
        if pytest_error:
            path, start_line, start_column = last_pytest_source or (None, None, None)
            found.append({
                "category": "pytest_assertion_detail", "path": path, "start_line": start_line,
                "start_column": start_column, "end_line": None, "end_column": None,
                "message": line, "source": "GitHub Actions job log",
            })
            continue
        generic_error = GENERIC_ERROR_RE.match(line)
        if generic_error:
            path, start_line = last_python_frame or (None, None)
            found.append({
                "category": "log_error_" + generic_error.group("kind").lower().replace(" ", "_"),
                "path": path, "start_line": start_line, "start_column": None,
                "end_line": None, "end_column": None, "message": line,
                "source": "GitHub Actions job log",
            })
            continue
        exception = EXCEPTION_RE.match(line)
        if exception:
            path, start_line = last_python_frame or (None, None)
            found.append({
                "category": "exception_" + exception.group("kind"), "path": path,
                "start_line": start_line, "start_column": None, "end_line": None, "end_column": None,
                "message": line, "source": "GitHub Actions job log",
            })
    return list({_canonical_json(entry): entry for entry in found}.values())


def _pr_link_sha(run: dict[str, Any], pr_number: int) -> str | None:
    links = run.get("pull_requests")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("number") == pr_number:
                head = link.get("head")
                if isinstance(head, dict) and isinstance(head.get("sha"), str):
                    return head["sha"]
    return None


def _matches_pr_run(run: dict[str, Any], pr: dict[str, Any], sha: str) -> bool:
    if run.get("event") != "pull_request" or run.get("head_branch") != pr.get("head", {}).get("ref"):
        return False
    links = run.get("pull_requests")
    linked_sha = _pr_link_sha(run, int(pr["number"]))
    if isinstance(links, list) and links:
        # A non-empty association list that names another PR is authoritative:
        # never fall back to branch/SHA equality across different PR identities.
        return linked_sha == sha
    # Some GitHub events omit the association list entirely. In that case only
    # the exact executed SHA is acceptable; branch-name equality is insufficient.
    return run.get("head_sha") == sha


def _list_workflow_runs(slug: str, workflow_id: int, branch: str) -> list[dict[str, Any]]:
    workflow_ref = urllib.parse.quote(str(workflow_id), safe="")
    query = urllib.parse.urlencode({"branch": branch, "event": "pull_request"})
    return _pages(f"repos/{slug}/actions/workflows/{workflow_ref}/runs?{query}", "workflow_runs")


def _workflow_ids(slug: str) -> dict[str, int]:
    workflows = _pages(f"repos/{slug}/actions/workflows", "workflows")
    result: dict[str, int] = {}
    for configured in WORKFLOW_INVENTORY:
        matches = [w for w in workflows if w.get("name") == configured["name"] and w.get("state") == "active"]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
            raise DiagnosticError(f"Expected exactly one active workflow named {configured['name']!r}")
        result[configured["name"]] = int(matches[0]["id"])
    return result


def _list_runs_for_pr(slug: str, pr: dict[str, Any], sha: str,
                      workflow_ids: dict[str, int]) -> dict[str, dict[str, Any]]:
    branch = str(pr.get("head", {}).get("ref") or "")
    if not branch:
        raise DiagnosticError("Pull request has no head branch")
    selected: dict[str, dict[str, Any]] = {}
    for spec in WORKFLOW_INVENTORY:
        bases = spec["base_branches"]
        if bases is not None and pr.get("base", {}).get("ref") not in bases:
            continue
        candidates = [
            run for run in _list_workflow_runs(slug, workflow_ids[spec["name"]], branch)
            if _matches_pr_run(run, pr, sha)
        ]
        candidates.sort(key=lambda run: (
            int(run.get("run_number") or 0), int(run.get("run_attempt") or 1),
            str(run.get("updated_at") or ""), int(run.get("id") or 0),
        ))
        if candidates:
            selected[spec["name"]] = candidates[-1]
    return selected


def _jobs_for_run(slug: str, run_id: int) -> list[dict[str, Any]]:
    return _pages(f"repos/{slug}/actions/runs/{run_id}/jobs?filter=latest", "jobs")


def _all_jobs_terminal(jobs: list[dict[str, Any]], run: dict[str, Any]) -> bool:
    for job in jobs:
        if job.get("status") != "completed":
            return False
        steps = job.get("steps") if isinstance(job.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                return False
            status = step.get("status")
            conclusion = _normal_conclusion(step.get("conclusion"))
            if status and status != "completed":
                return False
            if not status and not conclusion:
                return False
    # A green workflow with no visible jobs may be an eventually-consistent API
    # view. Non-success workflows with zero jobs remain reportable at workflow level.
    return bool(jobs) or _normal_conclusion(run.get("conclusion")) != "success"


def _annotations(slug: str, checkrun_url: Any) -> list[dict[str, Any]]:
    if checkrun_url is None:
        return []
    if not isinstance(checkrun_url, str):
        raise DiagnosticError("GitHub returned a malformed check-run URL")
    parsed = urllib.parse.urlsplit(checkrun_url)
    expected_prefix = f"/repos/{slug}/check-runs/"
    suffix = parsed.path[len(expected_prefix):] if parsed.path.startswith(expected_prefix) else ""
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or not suffix.isdigit() or parsed.query or parsed.fragment:
        raise DiagnosticError("GitHub returned an unexpected check-run URL; annotations were not fetched")
    path = parsed.path.lstrip("/") + "/annotations"
    values: list[dict[str, Any]] = []
    for page in range(1, 51):
        payload, _, _ = _request(_api_url(path + "?" + urllib.parse.urlencode({"per_page": 100, "page": page})))
        try:
            batch = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiagnosticError("GitHub returned invalid check annotation data") from exc
        if not isinstance(batch, list):
            raise DiagnosticError("GitHub returned malformed check annotation data")
        values.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return values
    raise DiagnosticError("GitHub annotation pagination safety limit reached")


def _add_structured_outcomes(records: dict[str, dict[str, Any]], repository: str,
                             pr_number: int, sha: str, workflow_name: str,
                             run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    conclusion = _normal_conclusion(run.get("conclusion"))
    if conclusion and conclusion != "success":
        record = _base_record(repository, pr_number, sha, workflow_name, run)
        record.update({"conclusion": conclusion, "category": "workflow_outcome",
                       "source": "GitHub Actions workflow-run API"})
        _add_record(records, record)
    for job in jobs:
        job_conclusion = _normal_conclusion(job.get("conclusion"))
        if job_conclusion and job_conclusion != "success":
            record = _base_record(repository, pr_number, sha, workflow_name, run, job=job)
            record.update({"conclusion": job_conclusion, "category": "job_outcome",
                           "source": "GitHub Actions job API"})
            _add_record(records, record)
        steps = job.get("steps") if isinstance(job.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_conclusion = _normal_conclusion(step.get("conclusion"))
            if step_conclusion and step_conclusion != "success":
                record = _base_record(repository, pr_number, sha, workflow_name, run, job=job, step=step)
                record.update({"conclusion": step_conclusion, "category": "step_outcome",
                               "source": "GitHub Actions step API"})
                _add_record(records, record)


def _add_annotations(records: dict[str, dict[str, Any]], repository: str,
                     pr_number: int, sha: str, workflow_name: str,
                     run: dict[str, Any], job: dict[str, Any]) -> None:
    for annotation in _annotations(repository, job.get("check_run_url")):
        level = str(annotation.get("annotation_level") or "").lower()
        if level not in ANNOTATION_LEVELS:
            continue
        record = _base_record(repository, pr_number, sha, workflow_name, run, job=job)
        record.update({
            "outcome_level": "annotation", "conclusion": level, "category": "check_annotation",
            "path": annotation.get("path"), "start_line": annotation.get("start_line"),
            "start_column": annotation.get("start_column"), "end_line": annotation.get("end_line"),
            "end_column": annotation.get("end_column"), "message": annotation.get("message"),
            "source": "GitHub check annotation API",
        })
        _add_record(records, record)


def _decode_job_log_payload(payload: bytes, job_id: int) -> str:
    if len(payload) > MAX_LOG_ARCHIVE_BYTES:
        raise DiagnosticError(f"GitHub job log archive exceeds the processing limit for job {job_id}")

    # GitHub normally provides a ZIP containing one text file per step. Parse
    # in memory and never extract untrusted archive filenames to disk.
    is_zip = payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if not is_zip:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            pass

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            files = sorted(
                (entry for entry in archive.infolist() if not entry.is_dir()),
                key=lambda entry: entry.filename,
            )
            if sum(entry.file_size for entry in files) > MAX_EXPANDED_LOG_BYTES:
                raise DiagnosticError(
                    f"Expanded GitHub job log archive exceeds the processing limit for job {job_id}"
                )
            text_files = [
                entry for entry in files
                if entry.filename.lower().endswith((".txt", ".log"))
            ]
            if not text_files:
                raise DiagnosticError(
                    f"GitHub job log archive contains no recognized text logs for job {job_id}"
                )
            sections: list[str] = []
            for entry in text_files:
                try:
                    sections.append(archive.read(entry).decode("utf-8"))
                except (UnicodeDecodeError, zipfile.BadZipFile, RuntimeError, OSError):
                    raise DiagnosticError(
                        f"GitHub job log archive has an unreadable entry for job {job_id}"
                    ) from None
            return "\n".join(sections)
    except DiagnosticError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError):
        raise DiagnosticError(
            f"GitHub returned an unrecognized job log archive for job {job_id}"
        ) from None


def _job_log(slug: str, job_id: int) -> str | None:
    endpoint = _api_url(f"repos/{slug}/actions/jobs/{job_id}/logs")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + _token(),
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "Trade-Bot-CI-diagnostics",
    }

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(endpoint, headers=headers)
    location = ""
    payload = b""
    try:
        with opener.open(request, timeout=60) as response:
            payload = response.read(MAX_LOG_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code not in {301, 302, 303, 307, 308}:
            raise DiagnosticError(
                f"GitHub job log request failed ({exc.code}) for job {job_id}"
            ) from None
        location = str(exc.headers.get("Location") or "")
        if not location:
            raise DiagnosticError(
                f"GitHub returned a job log redirect without a destination for job {job_id}"
            ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DiagnosticError(f"GitHub job log request unavailable for job {job_id}") from None

    if location:
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise DiagnosticError(f"GitHub returned an unsafe log-download redirect for job {job_id}")
        # Follow the signed URL without forwarding the GitHub bearer credential.
        request = urllib.request.Request(location, headers={"User-Agent": "Trade-Bot-CI-diagnostics"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read(MAX_LOG_ARCHIVE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise DiagnosticError(f"Redirected job log download failed for job {job_id}") from None

    return _decode_job_log_payload(payload, job_id)


def _add_log_diagnostics(records: dict[str, dict[str, Any]], repository: str,
                         pr_number: int, sha: str, workflow_name: str,
                         run: dict[str, Any], job: dict[str, Any]) -> None:
    job_id = job.get("id")
    if not isinstance(job_id, int):
        return
    conclusion = _normal_conclusion(job.get("conclusion"))
    steps = job.get("steps") if isinstance(job.get("steps"), list) else []
    failing_steps = [
        step for step in steps if isinstance(step, dict)
        and _normal_conclusion(step.get("conclusion")) in LOG_FAILURES
    ]
    # Scan every completed job log on the GitHub-hosted collector, not only
    # failed jobs. Successful output is discarded by parse_log_lines unless it
    # contains an explicit diagnostic format; this also catches warnings and
    # source errors emitted by tools that nevertheless exit zero.
    log = _job_log(repository, job_id)
    if log is None:
        return
    identified_step = failing_steps[0] if len(failing_steps) == 1 else None
    for parsed in parse_log_lines(log):
        record = _base_record(repository, pr_number, sha, workflow_name, run, job=job, step=identified_step)
        is_annotation = parsed.get("category") == "check_annotation"
        record.update({
            "outcome_level": "annotation" if is_annotation else "log_diagnostic",
            "conclusion": parsed.get("annotation_level") if is_annotation else (
                conclusion if conclusion not in (None, "success", "skipped") else "diagnostic"
            ),
            "category": parsed["category"], "path": parsed["path"],
            "start_line": parsed["start_line"], "start_column": parsed["start_column"],
            "end_line": parsed["end_line"], "end_column": parsed["end_column"],
            "message": parsed["message"], "source": parsed["source"],
        })
        _add_record(records, record)


def _split_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if message is not None and not isinstance(message, str):
        message = str(message)
    chunks = [message] if not message else [
        message[i:i + MESSAGE_FRAGMENT_CHARS] for i in range(0, len(message), MESSAGE_FRAGMENT_CHARS)
    ]
    record_id = hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()
    fixed = {key: value for key, value in record.items() if key != "message"}
    return [{
        "record_id": record_id, "record": fixed, "message_part_index": index,
        "message_part_count": len(chunks), "message_fragment": chunk,
    } for index, chunk in enumerate(chunks)]


def _pack_pages(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    fragments = [fragment for record in records for fragment in _split_record(record)]
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for fragment in fragments:
        candidate = current + [fragment]
        if len(_canonical_json(candidate).encode("utf-8")) > REPORT_PAGE_BYTES:
            if not current:
                raise DiagnosticError("A diagnostic fragment exceeds GitHub Check Run report capacity")
            pages.append(current)
            current = [fragment]
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages


def _external_id(pr_number: int, sha: str) -> str:
    return f"{MASTER_EXTERNAL_PREFIX}{pr_number}:sha:{sha}"


def _page_external_id(pr_number: int, sha: str, page_number: int) -> str:
    return f"{PAGE_EXTERNAL_PREFIX}{pr_number}:{sha}:{page_number:04d}"


def _check_runs_for_sha(slug: str, sha: str) -> list[dict[str, Any]]:
    return _pages(f"repos/{slug}/commits/{urllib.parse.quote(sha, safe='')}/check-runs?filter=all", "check_runs")


def _ours(checkrun: dict[str, Any]) -> bool:
    app = checkrun.get("app")
    return isinstance(app, dict) and app.get("slug") == "github-actions"


def _upsert_check_run(slug: str, sha: str, name: str, external_id: str,
                      existing: dict[str, Any] | None, *, status: str,
                      output: dict[str, str], details_url: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name, "head_sha": sha, "external_id": external_id,
        "details_url": details_url, "status": status, "output": output,
    }
    if status == "completed":
        body["conclusion"] = "success"
    if existing and isinstance(existing.get("id"), int):
        result = _json(f"repos/{slug}/check-runs/{int(existing['id'])}", method="PATCH", body=body)
    else:
        result = _json(f"repos/{slug}/check-runs", method="POST", body=body)
    if not isinstance(result, dict) or not isinstance(result.get("id"), int):
        raise DiagnosticError(f"GitHub did not return a usable Check Run for {name!r}")
    return result


def _report_runs(slug: str, sha: str, pr_number: int) -> tuple[dict[str, Any] | None, dict[int, dict[str, Any]]]:
    master_external_id = _external_id(pr_number, sha)
    page_prefix = f"{PAGE_EXTERNAL_PREFIX}{pr_number}:{sha}:"
    masters: list[dict[str, Any]] = []
    pages: dict[int, dict[str, Any]] = {}
    for run in _check_runs_for_sha(slug, sha):
        if not _ours(run):
            continue
        external_id = run.get("external_id")
        if external_id == master_external_id and run.get("name") == MASTER_NAME:
            masters.append(run)
        elif isinstance(external_id, str) and external_id.startswith(page_prefix) and str(run.get("name", "")).startswith(PAGE_PREFIX):
            suffix = external_id[len(page_prefix):]
            if suffix.isdigit():
                index = int(suffix)
                current = pages.get(index)
                if current is None or str(run.get("updated_at", "")) > str(current.get("updated_at", "")):
                    pages[index] = run
    masters.sort(key=lambda run: (str(run.get("updated_at", "")), int(run.get("id") or 0)))
    return (masters[-1] if masters else None), pages


def _begin_master(slug: str, pr_number: int, sha: str, run_id: int, attempt: int) -> dict[str, Any]:
    master, _ = _report_runs(slug, sha, pr_number)
    summary = _canonical_json({
        "schema_version": 1, "status": "collecting", "pr": pr_number,
        "head_sha": sha, "collector_run_id": run_id, "collector_run_attempt": attempt,
    })
    return _upsert_check_run(
        slug, sha, MASTER_NAME, _external_id(pr_number, sha), master,
        status="in_progress", output={"title": "CI diagnostics collection", "summary": summary},
        details_url=f"https://github.com/{slug}/actions/runs/{run_id}",
    )


def _publish_report(slug: str, pr_number: int, sha: str, collector_run_id: int,
                    collector_attempt: int, master: dict[str, Any],
                    records: list[dict[str, Any]]) -> None:
    digest = hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
    generation = digest[:24]
    pages = _pack_pages(records)
    _, existing_pages = _report_runs(slug, sha, pr_number)
    details_url = f"https://github.com/{slug}/actions/runs/{collector_run_id}"
    for page_number, page_records in enumerate(pages, start=1):
        external_id = _page_external_id(pr_number, sha, page_number)
        text_payload = _canonical_json(page_records)
        if len(text_payload.encode("utf-8")) > REPORT_PAGE_BYTES:
            raise DiagnosticError(f"Report page {page_number} exceeds the configured output limit")
        result = _upsert_check_run(
            slug, sha, f"{PAGE_PREFIX}{page_number:04d}", external_id,
            existing_pages.get(page_number), status="completed",
            output={
                "title": "Non-success diagnostic records",
                "summary": _canonical_json({
                    "schema_version": 1, "generation_id": generation,
                    "page": page_number, "page_count": len(pages),
                }),
                "text": text_payload,
            },
            details_url=details_url,
        )
        if result.get("status") != "completed" or result.get("conclusion") != "success":
            raise DiagnosticError(f"Report page {page_number} was not finalized")

    manifest = {
        "schema_version": 1, "status": "finalized", "pr": pr_number, "head_sha": sha,
        "generation_id": generation, "diagnostic_count": len(records), "page_count": len(pages),
        "digest": digest, "collector_run_id": collector_run_id,
        "collector_run_attempt": collector_attempt,
    }
    updated = _upsert_check_run(
        slug, sha, MASTER_NAME, _external_id(pr_number, sha), master,
        status="completed", output={"title": "CI diagnostics report manifest", "summary": _canonical_json(manifest)},
        details_url=details_url,
    )
    if updated.get("status") != "completed" or updated.get("conclusion") != "success":
        raise DiagnosticError("The authoritative diagnostics manifest did not finalize")


def _load_current_pr(slug: str, pr_number: int) -> dict[str, Any]:
    payload = _json(f"repos/{slug}/pulls/{pr_number}")
    if not isinstance(payload, dict) or payload.get("number") != pr_number:
        raise DiagnosticError("GitHub did not return the requested pull request")
    if not isinstance(payload.get("head"), dict) or not isinstance(payload["head"].get("sha"), str):
        raise DiagnosticError("The pull request head SHA is unavailable")
    return payload


def _resolve_pr_from_event(slug: str, trigger: dict[str, Any]) -> dict[str, Any] | None:
    branch = trigger.get("head_branch")
    candidates: dict[int, dict[str, Any]] = {}
    links = trigger.get("pull_requests")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict) or not isinstance(link.get("number"), int):
                continue
            pull = _json(f"repos/{slug}/pulls/{int(link['number'])}", allow_404=True)
            if isinstance(pull, dict):
                candidates[int(pull["number"])] = pull
    if not candidates and isinstance(branch, str) and branch:
        head_repo = trigger.get("head_repository")
        owner_data = head_repo.get("owner") if isinstance(head_repo, dict) else None
        owner = str(owner_data.get("login") or slug.split("/", 1)[0]) if isinstance(owner_data, dict) else slug.split("/", 1)[0]
        query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}", "per_page": 100})
        pulls = _json(f"repos/{slug}/pulls?{query}")
        if isinstance(pulls, list):
            for pull in pulls:
                if isinstance(pull, dict) and isinstance(pull.get("number"), int):
                    candidates[int(pull["number"])] = pull
    valid: list[dict[str, Any]] = []
    for pull in candidates.values():
        if pull.get("state") != "open":
            continue
        if pull.get("head", {}).get("ref") != branch:
            continue
        sha = pull.get("head", {}).get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        linked_sha = _pr_link_sha(trigger, int(pull["number"]))
        if linked_sha and linked_sha != sha:
            continue
        if trigger.get("head_sha") == sha or linked_sha == sha:
            valid.append(pull)
    if len(valid) > 1:
        raise DiagnosticError("Workflow event matches more than one current pull request; refusing ambiguous correlation")
    return valid[0] if valid else None


def _ensure_head_unchanged(slug: str, pr_number: int, sha: str) -> None:
    current = _load_current_pr(slug, pr_number)
    if current.get("state") != "open" or current.get("head", {}).get("sha") != sha:
        raise DiagnosticError("Pull-request head changed or closed while collecting; stale report was not finalized")


def _collect() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        raise DiagnosticError("GITHUB_EVENT_PATH is unavailable")
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagnosticError("Workflow event payload is missing or malformed") from exc
    if not isinstance(event, dict):
        raise DiagnosticError("Workflow event payload is missing or malformed")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise DiagnosticError("collector execution is restricted to GitHub Actions runners")
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise DiagnosticError("GITHUB_REPOSITORY is missing or malformed")

    if event_name == "workflow_run":
        trigger = event.get("workflow_run")
        if not isinstance(trigger, dict):
            raise DiagnosticError("Expected a workflow_run completion event")
        if trigger.get("name") not in {entry["name"] for entry in WORKFLOW_INVENTORY}:
            return 0
        if trigger.get("status") != "completed" or trigger.get("event") != "pull_request":
            return 0
        pull = _resolve_pr_from_event(slug, trigger)
        if pull is None:
            return 0
        pr_number = int(pull["number"])
        sha = str(pull["head"]["sha"])
        if not _matches_pr_run(trigger, pull, sha):
            return 0
    else:
        return 0

    pr_number = int(pull["number"])
    sha = str(pull["head"]["sha"])
    try:
        collector_run_id = int(os.environ["GITHUB_RUN_ID"])
        collector_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    except (KeyError, ValueError) as exc:
        raise DiagnosticError("Collector run identity is unavailable") from exc

    master = _begin_master(slug, pr_number, sha, collector_run_id, collector_attempt)
    workflow_ids = _workflow_ids(slug)
    deadline = time.monotonic() + COLLECT_DEADLINE_SECONDS
    terminal_runs: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    while time.monotonic() < deadline:
        _ensure_head_unchanged(slug, pr_number, sha)
        selected = _list_runs_for_pr(slug, pull, sha, workflow_ids)
        all_terminal = True
        terminal_runs = {}
        for spec in WORKFLOW_INVENTORY:
            bases = spec["base_branches"]
            if bases is not None and pull.get("base", {}).get("ref") not in bases:
                continue
            name = spec["name"]
            run = selected.get(name)
            if not run or run.get("status") != "completed" or not _normal_conclusion(run.get("conclusion")):
                all_terminal = False
                break
            jobs = _jobs_for_run(slug, int(run["id"]))
            if not _all_jobs_terminal(jobs, run):
                all_terminal = False
                break
            terminal_runs[name] = (run, jobs)
        if all_terminal:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        pending = [
            spec["name"] for spec in WORKFLOW_INVENTORY
            if (spec["base_branches"] is None or pull.get("base", {}).get("ref") in spec["base_branches"])
            and spec["name"] not in terminal_runs
        ]
        raise DiagnosticError("Applicable workflow/job inventory did not reach terminal states; report remains unfinalized. Pending or absent: " + ", ".join(pending))

    _ensure_head_unchanged(slug, pr_number, sha)
    records_by_key: dict[str, dict[str, Any]] = {}
    for spec in WORKFLOW_INVENTORY:
        name = spec["name"]
        if name not in terminal_runs:
            continue
        run, jobs = terminal_runs[name]
        _add_structured_outcomes(records_by_key, slug, pr_number, sha, name, run, jobs)
        for job in jobs:
            _add_annotations(records_by_key, slug, pr_number, sha, name, run, job)
            _add_log_diagnostics(records_by_key, slug, pr_number, sha, name, run, job)
    records = sorted(records_by_key.values(), key=lambda item: (
        item.get("workflow_name") or "", int(item.get("workflow_run_id") or 0),
        int(item.get("job_id") or 0), item.get("outcome_level") or "",
        item.get("path") or "", int(item.get("start_line") or 0), item.get("message") or "",
    ))
    _ensure_head_unchanged(slug, pr_number, sha)
    _publish_report(slug, pr_number, sha, collector_run_id, collector_attempt, master, records)
    return 0


def _report_error(message: str, code: int = 2) -> int:
    sys.stderr.write(f"prfail: {message}\n")
    return code


def _canonical_invocation(argv: list[str]) -> int | None:
    if len(argv) == 2 and argv[0] == "--pr" and re.fullmatch(r"[1-9][0-9]*", argv[1]):
        try:
            return int(argv[1])
        except ValueError:
            return None
    return None


def _decode_report_pages(pr_number: int, sha: str, manifest: dict[str, Any],
                         pages_by_index: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    page_count = manifest.get("page_count")
    generation = manifest.get("generation_id")
    if not isinstance(page_count, int) or page_count < 0 or page_count > 9000:
        raise DiagnosticError("Report manifest has an invalid page count")
    if not isinstance(generation, str) or not generation:
        raise DiagnosticError("Report manifest has no generation identity")
    fragments_by_record: dict[str, dict[str, Any]] = {}
    for index in range(1, page_count + 1):
        page = pages_by_index.get(index)
        if not page:
            raise DiagnosticError(f"Report is incomplete: page {index} is missing")
        if page.get("status") != "completed" or page.get("conclusion") != "success" or page.get("head_sha") != sha:
            raise DiagnosticError(f"Report is incomplete: page {index} is not finalized on the requested SHA")
        output = page.get("output") if isinstance(page.get("output"), dict) else {}
        try:
            page_meta = json.loads(str(output.get("summary") or ""))
            entries = json.loads(str(output.get("text") or ""))
        except json.JSONDecodeError as exc:
            raise DiagnosticError(f"Report is malformed: page {index} is not valid JSON") from exc
        if not isinstance(page_meta, dict) or page_meta.get("generation_id") != generation or page_meta.get("page") != index or page_meta.get("page_count") != page_count:
            raise DiagnosticError(f"Report is inconsistent: page {index} belongs to a different generation")
        if not isinstance(entries, list):
            raise DiagnosticError(f"Report is malformed: page {index} has no record list")
        for fragment in entries:
            if not isinstance(fragment, dict) or not isinstance(fragment.get("record_id"), str):
                raise DiagnosticError(f"Report is malformed: page {index} contains an invalid diagnostic fragment")
            record_id = fragment["record_id"]
            fixed = fragment.get("record")
            part_index = fragment.get("message_part_index")
            part_count = fragment.get("message_part_count")
            message_fragment = fragment.get("message_fragment")
            if not isinstance(fixed, dict) or not isinstance(part_index, int) or not isinstance(part_count, int) or part_count < 1 or not (0 <= part_index < part_count):
                raise DiagnosticError(f"Report is malformed: page {index} contains invalid message-fragment metadata")
            state = fragments_by_record.setdefault(record_id, {"record": fixed, "parts": {}, "count": part_count})
            if state["record"] != fixed or state["count"] != part_count or part_index in state["parts"]:
                raise DiagnosticError(f"Report is inconsistent: diagnostic {record_id} is duplicated or conflicting")
            if message_fragment is not None and not isinstance(message_fragment, str):
                raise DiagnosticError(f"Report is malformed: diagnostic {record_id} has an invalid message")
            state["parts"][part_index] = message_fragment

    records: list[dict[str, Any]] = []
    for record_id, state in fragments_by_record.items():
        if len(state["parts"]) != state["count"] or sorted(state["parts"]) != list(range(state["count"])):
            raise DiagnosticError(f"Report is incomplete: diagnostic {record_id} has missing message fragments")
        parts = [state["parts"][index] for index in range(state["count"])]
        message = None if all(part is None for part in parts) else "".join(part or "" for part in parts)
        record = dict(state["record"])
        record["message"] = message
        if hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest() != record_id:
            raise DiagnosticError(f"Report integrity check failed for diagnostic {record_id}")
        if record.get("pr") != pr_number or record.get("head_sha") != sha:
            raise DiagnosticError("Report contains diagnostics for a different PR or head SHA")
        for field in ("start_line", "start_column", "end_line", "end_column"):
            value = record.get(field)
            if value is not None and (not isinstance(value, int) or value < 1):
                raise DiagnosticError(f"Report contains an invalid source location field: {field}")
        records.append(record)
    records.sort(key=lambda item: (
        item.get("workflow_name") or "", int(item.get("workflow_run_id") or 0),
        int(item.get("job_id") or 0), item.get("outcome_level") or "",
        item.get("path") or "", int(item.get("start_line") or 0), item.get("message") or "",
    ))
    if len(records) != manifest.get("diagnostic_count"):
        raise DiagnosticError("Report diagnostic count does not match its manifest")
    if hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest() != manifest.get("digest"):
        raise DiagnosticError("Report digest does not match its manifest")
    return records


def _retrieve(pr_number: int) -> int:
    try:
        slug = _repo_slug()
        pull = _load_current_pr(slug, pr_number)
        sha = str(pull["head"]["sha"])
        master, pages = _report_runs(slug, sha, pr_number)
        if master is None:
            raise DiagnosticError("No finalized diagnostics report exists for the current PR head SHA")
        if master.get("status") != "completed" or master.get("conclusion") != "success":
            raise DiagnosticError("Diagnostics report is not finalized")
        output = master.get("output") if isinstance(master.get("output"), dict) else {}
        try:
            manifest = json.loads(str(output.get("summary") or ""))
        except json.JSONDecodeError as exc:
            raise DiagnosticError("Diagnostics report manifest is malformed") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("status") != "finalized":
            raise DiagnosticError("Diagnostics report is absent, incomplete, or malformed")
        if manifest.get("pr") != pr_number or manifest.get("head_sha") != sha or master.get("head_sha") != sha:
            raise DiagnosticError("Diagnostics report is stale or belongs to a different pull request")
        records = _decode_report_pages(pr_number, sha, manifest, pages)
        # The PR can advance while pages are being fetched. Never return an
        # otherwise valid report after the requested PR has moved to a new head.
        latest = _load_current_pr(slug, pr_number)
        if latest.get("state") != "open" or latest.get("head", {}).get("sha") != sha:
            raise DiagnosticError("Pull-request head changed during report retrieval; retrieve the new head's report")
        if records:
            sys.stdout.write(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
        return 0
    except DiagnosticError as exc:
        return _report_error(str(exc))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _report_error(f"report could not be validated ({type(exc).__name__})")


def main_prfail(argv: list[str]) -> int:
    pr_number = _canonical_invocation(argv)
    if pr_number is None:
        return _report_error("usage: ./scripts/prfail --pr <positive-decimal-PR-number>")
    return _retrieve(pr_number)


def main_collector() -> int:
    global _COLLECTOR_CONTEXT
    _COLLECTOR_CONTEXT = True
    try:
        return _collect()
    except DiagnosticError as exc:
        sys.stderr.write(f"CI diagnostics collection not finalized: {exc}\n")
        return 1
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError) as exc:
        sys.stderr.write(f"CI diagnostics collection not finalized: {type(exc).__name__}\n")
        return 1
    finally:
        _COLLECTOR_CONTEXT = False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        raise SystemExit(main_collector())
    raise SystemExit(main_prfail(sys.argv[1:]))

