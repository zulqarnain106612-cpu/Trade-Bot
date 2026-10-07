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

# Every measured file must reach this, counting branch coverage. Set below
# the observed minimum across the 248 measured files (src/models/gru.py at
# 91.67%) so the floor bites on a real regression rather than on today's
# weakest file, which is why EXCEPTIONS is empty.
MIN_PERCENT = 90.0

# path -> (floor, reason). Keep the reason concrete; "hard to test" is not one.
EXCEPTIONS: dict[str, tuple[float, str]] = {}


def _coverage_json() -> dict:
    """Return coverage's JSON report, generated from the existing .coverage."""
    # check=False: a failure here means "pytest has not run yet", and the
    # SystemExit below says so. CalledProcessError would not.
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-", "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
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


def _missing_ranges(entry: dict, cap: int = 24) -> str:
    """
    The uncovered lines of one file, as compact ranges.

    The percentage alone says a file is short and not what to write a test
    for, and the only other source is the job log -- which the CI failure
    notice exists to avoid reading. This check is the step that fails, so its
    output is what reaches the notice; it has to carry the lines.
    """
    lines = sorted(entry.get("missing_lines") or [])
    if not lines:
        return ""
    groups: list[tuple[int, int]] = []
    for n in lines:
        if groups and n == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], n)
        else:
            groups.append((n, n))
    shown = [f"{a}" if a == b else f"{a}-{b}" for a, b in groups[:cap]]
    more = "" if len(groups) <= cap else f", +{len(groups) - cap} more"
    return ", ".join(shown) + more


def main() -> int:
    report = _coverage_json()
    failures: list[str] = []
    stale: list[str] = []

    for path, entry in sorted(report["files"].items()):
        pct = _percent(entry["summary"])
        floor, reason = EXCEPTIONS.get(path, (MIN_PERCENT, ""))
        if pct + 1e-9 < floor:
            note = f" (exception: {reason})" if reason else ""
            missing = _missing_ranges(entry)
            where = f" -- missing {missing}" if missing else ""
            failures.append(f"  {path}: {pct:.2f}% < {floor:.0f}%{note}{where}")
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
