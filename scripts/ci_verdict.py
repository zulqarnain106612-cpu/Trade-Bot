#!/usr/bin/env python3
"""
The only CI answer an agent ever receives.

Whatever an agent executes -- a GitHub tool, a shell command, a URL -- when
the request is for CI, logs, reports, run records or monitoring, the
PreToolUse hook answers with this module's verdict and the underlying data
never reaches the model.

The verdict is one of exactly two things:

* every check on the PR head is green -> the literal line ``merged successfully``
* anything is failed, cancelled, timed out or otherwise not green -> only the
  log lines that define the failure, each prefixed with the workflow or job
  that produced it, and nothing else

The skim happens here, in the mechanism, not in the model: this module reads
the run records, and the agent receives the extracted lines only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = "https://api.github.com/"
GREEN = "merged successfully"

# A line "defines the failure" when it names one. Word boundaries keep
# "0 errors" style summaries from dragging the whole log in.
FAILURE_RE = re.compile(
    r"(?i)\b(error|errors|failed|failing|failure|failures|skipped|cancel|cancelled|"
    r"canceled|timed\s+out|timeout|traceback|assertionerror|exit\s+code\s+[1-9])\b"
)
# GitHub prefixes every log line with an ISO timestamp; it is not part of the
# failure and is stripped before the line is handed over.
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")
NOT_GREEN = {"failure", "cancelled", "canceled", "timed_out", "action_required", "stale"}

# Bounded so a pathological log cannot flood the context. Lines are deduped
# first, so this is a ceiling on distinct failure lines, not on log volume.
MAX_LINES = 80
MAX_JOBS = 6


class VerdictError(RuntimeError):
    """The verdict could not be established; the caller must fail closed."""


def _token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    # Same machine, same user: the configured GitHub MCP credential.
    try:
        config = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        header = config["mcpServers"]["github"]["headers"]["Authorization"]
        return header.split()[-1]
    except Exception:
        return ""


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=15
    )
    if proc.returncode:
        raise VerdictError("git command failed")
    return proc.stdout.strip()


def _repo_slug() -> str:
    remote = _git("remote", "get-url", "origin").removesuffix(".git")
    if remote.startswith("git@github.com:"):
        return remote.split(":", 1)[1]
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1].lstrip("/")
    raise VerdictError("origin is not a GitHub repository")


def _request(url: str, *, raw: bool = False, timeout: int = 20) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Trade-Bot-ci-verdict",
    }
    token = _token()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return payload.decode("utf-8", errors="replace") if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        raise VerdictError(f"GitHub API {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VerdictError("GitHub API unavailable") from exc


def _api(path: str, **kwargs: Any) -> Any:
    return _request(API_ROOT + path.lstrip("/"), **kwargs)


def resolve_pr(hint: Any = None) -> tuple[int | None, str]:
    """The PR this request is about, and its head sha."""
    slug = _repo_slug()
    if isinstance(hint, int) and hint > 0:
        pull = _api(f"repos/{slug}/pulls/{hint}")
        return int(pull["number"]), str(pull["head"]["sha"])
    sha = _git("rev-parse", "HEAD")
    for pull in _api(f"repos/{slug}/commits/{sha}/pulls"):
        if pull.get("state") == "open":
            return int(pull["number"]), str(pull.get("head", {}).get("sha") or sha)
    return None, sha


def _failure_lines(slug: str, sha: str) -> list[str]:
    runs = _api(f"repos/{slug}/actions/runs?head_sha={sha}&per_page=20").get("workflow_runs", [])
    if not runs:
        return []
    runs.sort(key=lambda run: str(run.get("created_at", "")), reverse=True)
    lines: list[str] = []
    seen: set[str] = set()
    jobs_read = 0
    for run in runs:
        if run.get("conclusion") not in NOT_GREEN:
            continue
        workflow = str(run.get("name") or run.get("path") or "workflow")
        jobs = _api(f"repos/{slug}/actions/runs/{run['id']}/jobs?per_page=50").get("jobs", [])
        for job in jobs:
            if job.get("conclusion") not in NOT_GREEN:
                continue
            if jobs_read >= MAX_JOBS:
                return lines
            jobs_read += 1
            try:
                log = _request(
                    f"{API_ROOT}repos/{slug}/actions/jobs/{job['id']}/logs", raw=True, timeout=25
                )
            except VerdictError:
                continue
            label = f"{workflow} / {job.get('name', 'job')}"
            for entry in log.splitlines():
                text = TIMESTAMP_RE.sub("", entry).strip()
                if not text or not FAILURE_RE.search(text):
                    continue
                line = f"{label}: {text}"
                if line in seen:
                    continue
                seen.add(line)
                lines.append(line)
                if len(lines) >= MAX_LINES:
                    return lines
    return lines


def verdict(pr_hint: Any = None) -> str:
    """The complete text the agent receives. Never raw records."""
    slug = _repo_slug()
    number, sha = resolve_pr(pr_hint)
    check_runs = _api(f"repos/{slug}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
    combined = _api(f"repos/{slug}/commits/{sha}/status")

    not_green = [
        run
        for run in check_runs
        if run.get("status") != "completed" or run.get("conclusion") in NOT_GREEN
    ]
    # The legacy combined-status API reports "pending" for a commit that has no
    # statuses at all, which is the normal case for a checks-only repository.
    # Treating that as not-green would make "merged successfully" unreachable.
    combined_state = str(combined.get("state", "")).lower()
    has_statuses = int(combined.get("total_count", 0) or 0) > 0
    state_bad = combined_state in {"failure", "error"} or (
        combined_state == "pending" and has_statuses
    )
    if not not_green and not state_bad:
        return GREEN

    lines = _failure_lines(slug, sha)
    if lines:
        return "\n".join(lines)
    # Not green but no log line names it: report the checks themselves, still
    # as failure-defining lines and nothing more.
    named = [
        f"{run.get('name', 'check')}: {run.get('conclusion') or run.get('status')}"
        for run in not_green
    ][:MAX_LINES]
    if named:
        return "\n".join(named)
    target = f"pull request {number}" if number else f"HEAD {sha[:12]}"
    return f"{target}: checks not green"


def main(argv: list[str]) -> int:
    hint = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
    try:
        sys.stdout.write(verdict(hint) + "\n")
    except VerdictError as exc:
        sys.stdout.write(f"ci verdict unavailable: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
