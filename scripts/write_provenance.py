#!/usr/bin/env python3
"""
SUP-004 — an artifact that cannot say where it came from.

After an incident the first question is "what exactly was running?", and the
usual answer is a guess assembled from a deployment timestamp and somebody's
memory of which branch was current. This writes the answer into the artifact
instead: version, commit, build time, builder, and a digest of every file in
the bundle.

`--verify` is the half that matters. A provenance file is trivially easy to
produce and just as easy to produce *wrongly* -- empty fields, a commit from
a cached checkout, a digest list that does not match the bytes beside it. So
the release workflow writes the record in one job and verifies it in another,
from the uploaded artifact rather than from the build directory, which is the
only way to notice that what was uploaded is not what was described.

The signed attestation (`actions/attest-build-provenance`) is the
cryptographic half and lives in the workflow. This file is the descriptive
half: the attestation proves *who* built the bytes, this says *what they
are*.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Fields that must be present and non-empty in a verified record. Listed
# rather than inferred: a record that silently loses a field when an
# environment variable is unset is the failure this guards against.
REQUIRED_FIELDS = (
    "schema_version",
    "version",
    "commit",
    "built_at",
    "builder",
    "workflow_run",
    "files",
)


def _git(*args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _version() -> str:
    # The tag if this is a tagged build, otherwise `describe`, otherwise the
    # short commit. Never "unknown": a version field that can be empty is a
    # field that will be empty on the one build anybody needs to identify.
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        return ref.removeprefix("refs/tags/")
    described = _git("describe", "--tags", "--always", "--dirty")
    return described or _git("rev-parse", "--short", "HEAD") or "unversioned"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_record(bundle: Path) -> dict[str, Any]:
    files = sorted(
        p for p in bundle.iterdir() if p.is_file() and p.name != "provenance.json"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "version": _version(),
        "commit": os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD") or "",
        "built_at": datetime.now(tz=UTC).isoformat(),
        "builder": os.environ.get("GITHUB_WORKFLOW", "local"),
        "workflow_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "repository": os.environ.get("GITHUB_REPOSITORY", _git("remote", "get-url", "origin")),
        "dry_run": os.environ.get("DRY_RUN", "").lower() == "true",
        "files": [{"name": p.name, "sha256": _digest(p), "bytes": p.stat().st_size} for p in files],
    }


def verify(record_path: Path) -> list[str]:
    """Return a list of problems; empty means the record describes the bundle."""
    problems: list[str] = []
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"provenance record is unreadable: {exc}"]

    if not isinstance(record, dict):
        return ["provenance record is not an object"]

    for field in REQUIRED_FIELDS:
        if field not in record or record[field] in ("", None, []):
            problems.append(f"missing or empty field: {field}")

    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version {record.get('schema_version')!r} is not {SCHEMA_VERSION}"
        )

    commit = str(record.get("commit", ""))
    if len(commit) != 40:
        problems.append(f"commit is not a full SHA: {commit!r}")

    expected_commit = os.environ.get("GITHUB_SHA")
    if expected_commit and commit and commit != expected_commit:
        # The case this catches: a cached or shallow checkout in the verifying
        # job, or a record carried over from a previous build.
        problems.append(f"commit {commit} does not match this run's {expected_commit}")

    bundle = record_path.parent
    for entry in record.get("files") or []:
        name = entry.get("name", "")
        path = bundle / name
        if not path.exists():
            problems.append(f"described file is missing from the bundle: {name}")
            continue
        actual = _digest(path)
        if actual != entry.get("sha256"):
            problems.append(f"digest mismatch for {name}")

    described = {entry.get("name") for entry in record.get("files") or []}
    present = {p.name for p in bundle.iterdir() if p.is_file() and p.name != "provenance.json"}
    for extra in sorted(present - described):
        # An undescribed file in a release bundle is the one nobody audits.
        problems.append(f"file in the bundle is not described: {extra}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", help="write a provenance record to this path")
    group.add_argument("--verify", help="verify an existing provenance record")
    args = parser.parse_args(argv)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        record = build_record(out.parent)
        out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {out}: version={record['version']} files={len(record['files'])}")
        return 0

    problems = verify(Path(args.verify))
    if problems:
        print("[FAIL] provenance record does not describe this bundle")
        for problem in problems:
            print(f"         {problem}")
        return 1
    print("[ok  ] provenance record matches the bundle")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
