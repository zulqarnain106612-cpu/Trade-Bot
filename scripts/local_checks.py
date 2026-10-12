#!/usr/bin/env python3
"""Expose only the current HEAD SHA-bound CI notice; local checks are disabled."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# REG-0024: run as a script, sys.path[0] is scripts/, so prepare()'s
# `from src.agent_control ...` import needs the repository root added.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_DIR = ROOT / ".claude" / ".hook-state"
PLAN = STATE_DIR / "local_check_plan.json"
TEST_PATH_RE = re.compile(r"(?:FAILED|ERROR)\s+((?:tests|src)/[A-Za-z0-9_./-]+\.py)")
FILE_PATH_RE = re.compile(r'File "(?:[^"]*/)?((?:tests|src)/[A-Za-z0-9_./-]+\.py)", line \d+')


def die(message: str) -> None:
    raise SystemExit(message)


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode:
        die(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def repo_slug() -> str:
    remote = git("remote", "get-url", "origin").removesuffix(".git")
    if remote.startswith("git@github.com:"):
        return remote.split(":", 1)[1]
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1].lstrip("/")
    die("origin is not a GitHub repository")


def api(path: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "Trade-Bot-local-checks",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        die(f"GitHub API {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as exc:
        die(f"GitHub API unavailable: {exc.reason}")


def current_pr(sha: str) -> dict[str, Any]:
    prs = api(f"repos/{repo_slug()}/commits/{sha}/pulls")
    matches = [
        pr for pr in prs
        if pr.get("state") == "open" and pr.get("head", {}).get("sha") == sha
    ]
    if not matches:
        die(f"{sha[:12]} is not the head of an open PR")
    return matches[0]


def latest_notice(pr_number: int, sha: str) -> str:
    comments = api(f"repos/{repo_slug()}/issues/{pr_number}/comments?per_page=100&sort=updated&direction=desc")
    marker = "<!-- ci-notice -->"
    sha_marker = f"<!-- ci-notice-sha:{sha} -->"
    # Only the notice for the exact current HEAD is exposed to the agent.
    matches = [
        item for item in comments
        if item.get("user", {}).get("login") == "github-actions[bot]"
        and marker in item.get("body", "")
        and sha_marker in item.get("body", "")
    ]
    latest = max(matches, key=lambda item: item.get("updated_at", ""), default=None)
    return latest.get("body", "") if latest else ""


def test_paths(comment: str) -> list[str]:
    paths = set(TEST_PATH_RE.findall(comment))
    paths.update(FILE_PATH_RE.findall(comment))
    return sorted(paths)


def prepare() -> int:
    # Fail closed: a stale notice must never survive when HEAD has no new
    # SHA-bound comment or GitHub is temporarily unavailable.
    PLAN.unlink(missing_ok=True)
    sha = git("rev-parse", "HEAD")
    pr = current_pr(sha)
    notice = latest_notice(int(pr["number"]), sha)
    if not notice:
        die(
            "CI diagnostic notice is not available for this HEAD; raw CI run data is intentionally unavailable"
        )
    from src.agent_control.reliability import record_ci_from_notice

    record = record_ci_from_notice(sha=sha, pr=int(pr["number"]), notice=notice)
    visible_notice = "\n".join(
        line for line in notice.splitlines()
        if not line.startswith("<!-- ci-notice")
    )
    if record["status"] == "green":
        PLAN.unlink(missing_ok=True)
        print(visible_notice)
        return 0
    # Record the single check label for reliability evidence, never as a
    # local execution allowlist.
    failed = [
        f"{match.group(1).strip()} / {match.group(2).strip()}"
        for match in re.finditer(
            r"^\*\*(.+?) / (.+?)\*\* — (?:failure|cancelled|timed_out|neutral|skipped|action_required|stale|no verdict)",
            notice,
            re.MULTILINE,
        )
    ]
    failed = sorted(set(failed))
    if not failed:
        die("CI notice is non-green but contains no actionable failed-check identity")
    plan = {
        "version": 2,
        "pr": int(pr["number"]),
        "source_sha": sha,
        "failed_checks": failed,
        "test_paths": test_paths(notice),
        "notice": notice[:6000],
        "created_at": time.time(),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    # Emit only the visible body of the one exact notice. No arbitrary PR
    # comments, workflow metadata, or local execution advice enters context.
    print(visible_notice)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare")
    sub.add_parser("run", help="disabled: checks execute only in GitHub Actions")
    args = parser.parse_args(argv)
    if args.action == "run":
        die("local check execution is disabled by repository policy; GitHub Actions is the sole test/build/check execution path")
    return prepare()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
