#!/usr/bin/env python3
"""
Fail unless every job a workflow gate depends on reported success.

GitHub has no built-in "all checks must be green" rule. Branch protection
requires *named* checks, so a job that never runs -- skipped by an `if:`, or
never reached because an earlier job failed -- satisfies a required check by
being absent, and a merge can go through with work silently undone. A neutral
or skipped conclusion is not a pass; this script is what says so.

Each workflow ends in a `gate` job that `needs:` every other job in it, runs
with `if: always()` so it executes even when something upstream failed, and
calls this script with `toJSON(needs)` in the environment. The gate is the one
check to mark required in branch protection: it turns "every job in this
workflow was green" into a single signal that cannot be satisfied by absence.

Environment:
    NEEDS           JSON object from ${{ toJSON(needs) }}. Maps job id to an
                    object with a "result" of success/failure/cancelled/skipped.
    ALLOW_SKIPPED   Optional comma-separated job ids permitted to skip, for
                    jobs whose `if:` legitimately excludes them on some events
                    (a job needing secrets on a fork pull request, say). Each
                    entry needs a comment in the workflow saying why. Anything
                    not named here that skips fails the gate.

Exit status:
    0   every job succeeded, or skipped while explicitly allowed to
    1   any job failed, was cancelled, or skipped without being allowed
    2   the environment is malformed -- treated as a failure, never a pass,
        because a gate that cannot evaluate has not verified anything
"""

from __future__ import annotations

import json
import os
import sys

#: Conclusions that are not a pass. GitHub reports "skipped" for a job whose
#: `if:` was false and for one whose dependency failed; both mean the work did
#: not happen, which is exactly what the gate exists to catch.
NOT_SUCCESS = ("failure", "cancelled", "skipped")

_ORDER = {"failure": 0, "cancelled": 1, "skipped": 2, "success": 3}


def parse_allow_skipped(raw: str | None) -> set[str]:
    """Parse ALLOW_SKIPPED into a set of job ids, tolerating spacing."""
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def evaluate(needs: dict, allow_skipped: set[str]) -> tuple[list[str], list[str]]:
    """
    Split jobs into (offenders, allowed_skips).

    Offenders are reported in severity order -- failures first -- so the top of
    the log names the thing most likely to be the cause rather than whichever
    job sorted first alphabetically.
    """
    offenders: list[str] = []
    allowed: list[str] = []

    for job_id, payload in needs.items():
        result = (payload or {}).get("result", "missing")
        if result == "success":
            continue
        if result == "skipped" and job_id in allow_skipped:
            allowed.append(job_id)
            continue
        offenders.append(job_id)

    offenders.sort(key=lambda j: (_ORDER.get(needs[j].get("result", ""), 9), j))
    return offenders, sorted(allowed)


def render_table(needs: dict, allow_skipped: set[str]) -> str:
    """Every job and its result, so the log shows the whole picture at once."""
    if not needs:
        return "(no jobs)"
    width = max(len(job_id) for job_id in needs)
    lines = []
    for job_id in sorted(needs):
        result = (needs[job_id] or {}).get("result", "missing")
        note = ""
        if result == "skipped" and job_id in allow_skipped:
            note = "  (skip allowed)"
        lines.append(f"  {job_id:<{width}}  {result}{note}")
    return "\n".join(lines)


def main() -> int:
    raw = os.environ.get("NEEDS")
    if not raw:
        print("NEEDS is empty or unset. The gate cannot verify anything.", file=sys.stderr)
        print("Pass it as: NEEDS: ${{ toJSON(needs) }}", file=sys.stderr)
        return 2

    try:
        needs = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"NEEDS is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(needs, dict):
        print(f"NEEDS must be a JSON object, got {type(needs).__name__}.", file=sys.stderr)
        return 2

    if not needs:
        # A gate with no dependencies would pass unconditionally and give a
        # false all-clear, which is worse than having no gate at all.
        print("NEEDS is an empty object: this gate depends on no jobs.", file=sys.stderr)
        print("Add every job in the workflow to the gate's `needs:` list.", file=sys.stderr)
        return 2

    allow_skipped = parse_allow_skipped(os.environ.get("ALLOW_SKIPPED"))
    offenders, allowed = evaluate(needs, allow_skipped)

    print(f"Gate over {len(needs)} job(s):")
    print(render_table(needs, allow_skipped))

    if allowed:
        print(f"\nSkipped with explicit allowance: {', '.join(allowed)}")

    unused = sorted(allow_skipped - set(needs))
    if unused:
        # A stale allowance is a hole nobody is watching: the job was renamed
        # or removed, and the exemption silently outlived its reason.
        print(f"\nALLOW_SKIPPED names job(s) not in needs: {', '.join(unused)}")
        print("Remove them, or correct the id -- a stale allowance hides a gap.")

    if offenders:
        print("\nGate failed. These jobs did not succeed:", file=sys.stderr)
        for job_id in offenders:
            print(f"  {job_id}: {needs[job_id].get('result', 'missing')}", file=sys.stderr)
        print(
            "\nA skipped or cancelled job is not a pass. Fix the job, or -- only if "
            "the skip is correct for this event -- add it to ALLOW_SKIPPED with a "
            "comment in the workflow saying why.",
            file=sys.stderr,
        )
        return 1

    print("\nAll jobs green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
