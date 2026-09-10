#!/usr/bin/env python3
"""
Apply the version-controlled repository ruleset to GitHub.

The merge policy -- which checks must be green before a pull request can land
-- is a GitHub *setting*, not a file, so it cannot be enforced by committing
anything. That makes it the one part of this repository's rules that is
invisible to review: nobody can tell from a diff whether it changed, or drifted,
or was switched off.

This script closes that gap by keeping the policy in
``.github/rulesets/main-protection.json`` and applying it from there. The file
is the source of truth; running this makes GitHub match it.

Repository-scoped by construction: a ruleset applies to every push and every
pull request targeting the branches in its ``conditions``, on every branch,
for as long as it is active. It is not per-pull-request state.

Usage::

    python3 scripts/apply_repo_ruleset.py --dry-run     # show what would change
    python3 scripts/apply_repo_ruleset.py               # create or update
    python3 scripts/apply_repo_ruleset.py --check       # verify, change nothing

Environment:
    GITHUB_TOKEN  A token with the ``administration: write`` permission on the
                  repository. A default Actions token does NOT have it; use a
                  fine-grained PAT or a GitHub App installation token.
    GITHUB_REPOSITORY
                  "owner/repo". Set automatically inside Actions; pass
                  --repo locally.

Exit status:
    0  GitHub matches the file (``--check``), or the apply succeeded
    1  GitHub differs from the file (``--check``), or the apply failed
    2  the environment is unusable -- no token, bad file, bad repo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULESET_PATH = PROJECT_ROOT / ".github" / "rulesets" / "main-protection.json"
API_ROOT = "https://api.github.com"


def strip_comments(value: Any) -> Any:
    """
    Remove underscore-prefixed keys before sending.

    The file carries its own rationale -- why each rule exists, why one check
    is deliberately absent -- because a policy nobody understands is a policy
    that gets deleted the first time it is inconvenient. GitHub rejects
    unknown keys, so they are stripped here rather than omitted from the file.
    """
    if isinstance(value, dict):
        return {k: strip_comments(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_comments(v) for v in value]
    return value


def load_ruleset() -> dict:
    if not RULESET_PATH.exists():
        raise FileNotFoundError(f"ruleset not found: {RULESET_PATH}")
    with RULESET_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    payload = strip_comments(raw)
    for required in ("name", "target", "enforcement", "rules"):
        if required not in payload:
            raise ValueError(f"ruleset is missing required key {required!r}")
    return payload


def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, Any]:
    """One GitHub API call. Returns (status, parsed body-or-None)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - fixed https API root
        f"{API_ROOT}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "trade-bot-ruleset-apply",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            text = response.read().decode("utf-8")
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, {"message": text}


def find_existing(owner: str, repo: str, name: str, token: str) -> dict | None:
    """Locate a ruleset by name, so applying twice updates instead of duplicating."""
    status, body = api("GET", f"/repos/{owner}/{repo}/rulesets", token)
    if status != 200:
        raise RuntimeError(f"listing rulesets failed ({status}): {_message(body)}")
    for entry in body or []:
        if entry.get("name") == name:
            return entry
    return None


def _message(body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("message", "")
        errors = body.get("errors")
        if errors:
            detail = f"{detail} -- {json.dumps(errors)}"
        return detail or json.dumps(body)
    return str(body)


def required_contexts(payload: dict) -> list[str]:
    for rule in payload.get("rules", []):
        if rule.get("type") == "required_status_checks":
            checks = rule.get("parameters", {}).get("required_status_checks", [])
            return [c.get("context", "") for c in checks]
    return []


def compare(payload: dict, remote: dict) -> list[str]:
    """
    Differences that matter, in the terms a reader cares about.

    Not a deep diff: GitHub echoes back defaults and ids the file never sets,
    so a naive comparison is always "different" and would make --check
    useless.
    """
    problems: list[str] = []

    if remote.get("enforcement") != payload.get("enforcement"):
        problems.append(
            f"enforcement: file says {payload.get('enforcement')!r}, "
            f"GitHub has {remote.get('enforcement')!r}"
        )

    want = set(required_contexts(payload))
    have = set(required_contexts(remote))
    for missing in sorted(want - have):
        problems.append(f"required check not enforced on GitHub: {missing!r}")
    for extra in sorted(have - want):
        problems.append(f"GitHub enforces a check the file does not list: {extra!r}")

    want_types = {r.get("type") for r in payload.get("rules", [])}
    have_types = {r.get("type") for r in remote.get("rules", [])}
    for missing in sorted(want_types - have_types):
        problems.append(f"rule missing on GitHub: {missing!r}")

    return problems


def resolve_repo(explicit: str | None) -> tuple[str, str]:
    value = explicit or os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in value:
        raise ValueError("repository must be 'owner/repo'; pass --repo or set GITHUB_REPOSITORY")
    owner, _, repo = value.partition("/")
    return owner, repo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/repo; defaults to $GITHUB_REPOSITORY")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail if GitHub does not match the file; change nothing",
    )
    args = parser.parse_args()

    try:
        payload = load_ruleset()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read ruleset: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Would apply to: {args.repo or os.environ.get('GITHUB_REPOSITORY', '?')}")
        print(json.dumps(payload, indent=2))
        print("\nRequired checks:")
        for context in required_contexts(payload):
            print(f"  - {context}")
        return 0

    try:
        owner, repo = resolve_repo(args.repo)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is not set.", file=sys.stderr)
        print(
            "It needs the 'administration: write' permission on the repository; "
            "the default Actions token does not have it.",
            file=sys.stderr,
        )
        return 2

    try:
        existing = find_existing(owner, repo, payload["name"], token)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        if existing is None:
            print(f"ruleset {payload['name']!r} does not exist on {owner}/{repo}.")
            print("Run: python3 scripts/apply_repo_ruleset.py")
            return 1
        status, remote = api("GET", f"/repos/{owner}/{repo}/rulesets/{existing['id']}", token)
        if status != 200:
            print(f"reading ruleset failed ({status}): {_message(remote)}", file=sys.stderr)
            return 1
        problems = compare(payload, remote)
        if problems:
            print("GitHub does not match .github/rulesets/main-protection.json:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"ruleset {payload['name']!r} matches the file.")
        return 0

    if existing is None:
        status, body = api("POST", f"/repos/{owner}/{repo}/rulesets", token, payload)
        action, ok = "created", status in (200, 201)
    else:
        status, body = api(
            "PUT", f"/repos/{owner}/{repo}/rulesets/{existing['id']}", token, payload
        )
        action, ok = "updated", status == 200

    if not ok:
        print(f"apply failed ({status}): {_message(body)}", file=sys.stderr)
        if status in (403, 404):
            print(
                "\n403/404 here usually means the token lacks 'administration: write', "
                "not that the repository is missing.",
                file=sys.stderr,
            )
        return 1

    print(f"ruleset {payload['name']!r} {action} on {owner}/{repo}.")
    print("Required checks now enforced:")
    for context in required_contexts(payload):
        print(f"  - {context}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
