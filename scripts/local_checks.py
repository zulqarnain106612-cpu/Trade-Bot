#!/usr/bin/env python3
"""Run only checks that were non-green on the last completed PR run."""

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
STATE_DIR = ROOT / ".claude" / ".hook-state"
PLAN = STATE_DIR / "local_check_plan.json"
PYTHON = (
    str(ROOT / ".venv" / "bin" / "python")
    if (ROOT / ".venv" / "bin" / "python").exists()
    else sys.executable
)

ALIASES = {
    "Python (lint + governance docs)": "lint",
    "Python tests": "tests",
    "Architecture Governance (crypto-architect)": "architecture",
    "Frontend (build)": "frontend",
    "Workflow lint": "workflow-lint",
    "Security": "security",
    "CI gate (all jobs green)": "ci-gate",
    "Security gate (all jobs green)": "security-gate",
    "CodeQL gate (all jobs green)": "codeql-gate",
    "Workflow lint gate (all jobs green)": "workflow-lint-gate",
}
SUPPORTED = {"lint", "tests", "architecture", "frontend", "workflow-lint"}
REQUIRED_GATES = {
    "ci-gate",
    "security-gate",
    "codeql-gate",
    "workflow-lint-gate",
}

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
    matches = [pr for pr in prs if pr.get("state") == "open"]
    if not matches:
        die(f"{sha[:12]} is not the head of an open PR")
    return matches[0]


def latest_notice(pr_number: int, sha: str) -> str:
    comments = api(f"repos/{repo_slug()}/issues/{pr_number}/comments?per_page=100")
    marker = "CI " + chr(96) + sha[:7] + chr(96)
    bodies = [
        item.get("body", "")
        for item in comments
        if "<!-- ci-notice -->" in item.get("body", "") and marker in item.get("body", "")
    ]
    return bodies[-1] if bodies else ""


def test_paths(comment: str) -> list[str]:
    paths = set(TEST_PATH_RE.findall(comment))
    paths.update(FILE_PATH_RE.findall(comment))
    return sorted(paths)


def key_for(name: str) -> str | None:
    for prefix, key in ALIASES.items():
        if name == prefix:
            return key
    for prefix, key in ALIASES.items():
        if name.startswith(prefix + " "):
            return key
    return None


def prepare() -> int:
    sha = git("rev-parse", "HEAD")
    pr = current_pr(sha)
    notice = latest_notice(int(pr["number"]), sha)
    if not notice:
        die(
            "CI diagnostic notice is not available for this HEAD; raw CI run data is intentionally unavailable"
        )
    from src.agent_control.reliability import record_ci_from_notice

    record = record_ci_from_notice(sha=sha, pr=int(pr["number"]), notice=notice)
    if record["status"] == "green":
        PLAN.unlink(missing_ok=True)
        print(f"GREEN PR#{pr['number']} {sha[:12]}: local execution locked")
        return 0
    failed = [
        match.group(2).strip()
        for match in re.finditer(
            r"^\*\*(.+?) / (.+?)\*\* — (?:failure|cancelled|timed_out|neutral|action_required|stale|no verdict)",
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
    unsupported = sorted(set(failed) - SUPPORTED)
    print("PLAN PR#{} {}: {}".format(pr["number"], sha[:12], ", ".join(failed)))
    if unsupported:
        print("unsupported locally: " + ", ".join(unsupported))
    return 0


def changed_paths(source: str) -> list[str]:
    values = [
        git("diff", "--name-only", source),
        git("diff", "--name-only", "--cached"),
    ]
    return sorted({line for value in values for line in value.splitlines() if line})


def ensure_fix(plan: dict[str, Any]) -> None:
    source = str(plan["source_sha"])
    if git("rev-parse", "HEAD") != source:
        return
    if not changed_paths(source):
        die("no fix detected since the failed CI commit; local checks are locked")


def command_run(name: str, command: list[str], cwd: Path = ROOT) -> int:
    log_dir = (
        Path(os.environ.get("CLAUDE_SCRATCHPAD") or os.environ.get("TMPDIR") or "/tmp")
        / "local-checks"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / (f"{name}-{int(time.time())}.log")
    start = time.monotonic()
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    output = proc.stdout + proc.stderr
    log.write_text(output, encoding="utf-8")
    signal = next(
        (
            line.strip()
            for line in output.splitlines()
            if re.search(r"(?i)(error|failed|failure|exception|traceback|fatal)", line)
        ),
        next(
            (line.strip() for line in output.splitlines() if line.strip()),
            f"exit {proc.returncode}",
        ),
    )
    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    print(f"{verdict} {name} ({time.monotonic() - start:.1f}s): {signal[:300]}")
    print(f"full output: {log}")
    return proc.returncode


def run_check(name: str) -> int:
    if not PLAN.exists():
        die("no failed-check plan; run local_checks.py prepare after a completed PR run")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if name not in plan.get("failed_checks", []):
        die(f"{name} was not non-green in the recorded PR run")
    ensure_fix(plan)

    if name not in SUPPORTED:
        die(f"{name} has no safe local reproduction; refusing a broader substitute")

    if name == "tests":
        paths = plan.get("test_paths", [])
        if not paths:
            die("CI notice named no test files; refusing a broad test suite")
        return command_run(
            "tests",
            [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-cov", *paths],
        )

    if name == "lint":
        paths = [p for p in changed_paths(str(plan["source_sha"])) if p.endswith(".py")]
        commands = [
            [PYTHON, "scripts/generate_math_docs.py", "--check"],
            [PYTHON, "scripts/generate_quality_docs.py", "--check"],
            [PYTHON, ".claude/skills/quality-engineering/scripts/qe_gate.py"],
        ]
        if paths:
            commands += [
                [PYTHON, "-m", "ruff", "check", *paths],
                [PYTHON, "-m", "ruff", "format", "--check", *paths],
            ]
        for i, command in enumerate(commands, 1):
            code = command_run(f"lint-{i}", command)
            if code:
                return code
        return 0

    if name == "architecture":
        return command_run(
            "architecture",
            ["bash", "scripts/arch_gate.sh", "--sarif", "/tmp/trade-bot-arch.sarif"],
        )

    if name == "frontend":
        code = command_run(
            "frontend-install",
            ["npm", "ci", "--prefer-offline", "--no-audit"],
            ROOT / "frontend",
        )
        if code:
            return code
        return command_run("frontend-build", ["npm", "run", "build"], ROOT / "frontend")

    if name == "workflow-lint":
        if subprocess.run(
            "command -v actionlint >/dev/null 2>&1", shell=True, check=False
        ).returncode:
            die("actionlint is not installed; refusing a substitute")
        return command_run("workflow-lint", ["actionlint"])

    die(f"unsupported local check: {name}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare")
    run = sub.add_parser("run")
    run.add_argument("check", choices=sorted(set(ALIASES.values())))
    args = parser.parse_args(argv)
    return prepare() if args.action == "prepare" else run_check(args.check)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
