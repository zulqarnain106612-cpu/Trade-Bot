#!/usr/bin/env python3
"""
Compact CI triage — the failures, and nothing else.

Reading CI is the main development loop on this project (CLAUDE.md forbids
running the gate locally), and the naive way to do it is expensive: a full
`gh run view --log-failed` is ~9k lines, and every one of those lines stays
in context for the rest of the session. This prints the handful of lines
that actually determine what to do next.

Usage:
    python3 scripts/ci_triage.py                 # latest run on current branch
    python3 scripts/ci_triage.py --branch main
    python3 scripts/ci_triage.py --run 30696113656
    python3 scripts/ci_triage.py --trace test_foo # + traceback for one test

Exit codes:
    0  — run succeeded
    1  — run failed (details printed)
    2  — run still in progress, or no run found
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


REPO = "zulqarnain106612-cpu/Trade-Bot"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PREFIX = re.compile(r"^.*?\t.*?\t[0-9T:.Z-]+ ")


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def _latest_run(branch: str) -> dict | None:
    out = _sh(
        "gh",
        "run",
        "list",
        "--branch",
        branch,
        "--workflow",
        "ci.yml",
        "--limit",
        "1",
        "--json",
        "databaseId,status,conclusion,headSha",
    )
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def _jobs(run_id: str) -> list[dict]:
    out = _sh("gh", "api", f"repos/{REPO}/actions/runs/{run_id}/jobs")
    return json.loads(out or "{}").get("jobs", [])


def _clean(line: str) -> str:
    return _ANSI.sub("", _PREFIX.sub("", line)).rstrip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch")
    ap.add_argument("--run")
    ap.add_argument("--trace", help="also print the traceback for this test name")
    args = ap.parse_args()

    if args.run:
        run_id = args.run
        status = conclusion = None
    else:
        branch = args.branch or _sh("git", "branch", "--show-current").strip()
        run = _latest_run(branch)
        if not run:
            print(f"no ci.yml run found for {branch}")
            return 2
        run_id, status, conclusion = (
            str(run["databaseId"]),
            run["status"],
            run["conclusion"],
        )
        print(f"run {run_id} on {branch}: {status} {conclusion or ''}".rstrip())

    jobs = _jobs(run_id)
    incomplete = False
    for job in jobs:
        verdict = job["conclusion"] or job["status"]
        if job["conclusion"] is None:
            incomplete = True
        print(f"  {job['name']}: {verdict}")
        for step in job.get("steps") or []:
            if step["conclusion"] == "failure":
                print(f"     failed step: {step['name']}")

    if incomplete:
        return 2
    if conclusion == "success" or all(j["conclusion"] == "success" for j in jobs):
        return 0

    log = _sh("gh", "run", "view", run_id, "--log-failed")
    if not log:
        print("  (log unavailable — it may still be uploading)")
        return 1

    lines = log.split("\n")

    # pytest's own summary is the densest signal available: one line per
    # failure with the assertion message already extracted.
    failures = [_clean(x) for x in lines if "FAILED tests/" in x]
    if failures:
        print(f"\n{len(failures)} test failure(s):")
        for f in failures:
            print("  " + f[f.index("FAILED tests/") + 7 :][:160])

    # Non-test failures (ruff, mypy, coverage floors) never reach that summary.
    if not failures:
        for pattern in ("error:", "Error:", "FAILED", "AssertionError"):
            hits = [_clean(x) for x in lines if pattern in x]
            if hits:
                print(f"\n{pattern} lines:")
                for h in hits[:15]:
                    print("  " + h[:160])
                break

    if args.trace:
        marker = next(
            (
                i
                for i, x in enumerate(lines)
                if args.trace in x and x.strip().startswith(("_", "="))
            ),
            None,
        )
        if marker is None:
            marker = next((i for i, x in enumerate(lines) if args.trace in x), None)
        if marker is not None:
            print(f"\ntraceback for {args.trace}:")
            for line in lines[marker : marker + 25]:
                text = _clean(line)
                if text:
                    print("  " + text[:160])

    return 1


if __name__ == "__main__":
    sys.exit(main())
