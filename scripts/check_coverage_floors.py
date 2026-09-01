#!/usr/bin/env python3
"""Enforce a per-file coverage floor on top of the repo-wide gate.

``--cov-fail-under=99`` is an *aggregate*: a single critical file can rot from
99% to 40% and the total barely moves, because the other twenty thousand
statements absorb it. This check closes that hole by requiring every measured
file to clear ``MIN_PERCENT`` on its own.

Files that legitimately sit lower are listed in ``EXCEPTIONS`` with the reason
and the number observed when the entry was added. The list is a ratchet: an
exception whose file now clears the floor is reported as stale so it gets
removed rather than quietly licensing a future regression.

Run after pytest, which leaves the ``.coverage`` data file behind:

    pytest -q
    python scripts/check_coverage_floors.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every measured file must reach this, counting branch coverage.
MIN_PERCENT = 90.0

# path -> (floor, reason). Keep the reason concrete; "hard to test" is not one.
EXCEPTIONS: dict[str, tuple[float, str]] = {}


def _coverage_json() -> dict:
    """Return coverage's JSON report, generated from the existing .coverage."""
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-", "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("could not read coverage data -- run pytest before this check\n")
    return json.loads(proc.stdout)


def _percent(summary: dict) -> float:
    """Line+branch percentage for one file, as coverage computes the total."""
    covered = summary["covered_lines"] + summary.get("covered_branches", 0)
    total = summary["num_statements"] + summary.get("num_branches", 0)
    return 100.0 if total == 0 else 100.0 * covered / total


def main() -> int:
    report = _coverage_json()
    failures: list[str] = []
    stale: list[str] = []

    for path, entry in sorted(report["files"].items()):
        pct = _percent(entry["summary"])
        floor, reason = EXCEPTIONS.get(path, (MIN_PERCENT, ""))
        if pct + 1e-9 < floor:
            note = f" (exception: {reason})" if reason else ""
            failures.append(f"  {path}: {pct:.2f}% < {floor:.0f}%{note}")
        elif path in EXCEPTIONS and pct + 1e-9 >= MIN_PERCENT:
            stale.append(f"  {path}: {pct:.2f}% now clears the {MIN_PERCENT:.0f}% floor")

    if stale:
        print("Stale exceptions -- delete these entries from EXCEPTIONS:")
        print("\n".join(stale))

    if failures:
        print(f"\nFiles below their coverage floor ({len(failures)}):")
        print("\n".join(failures))
        return 1

    if stale:
        return 1

    print(f"All {len(report['files'])} measured files clear their coverage floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
