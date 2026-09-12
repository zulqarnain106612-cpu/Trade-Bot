#!/usr/bin/env python3
"""
GOV-009, GOV-010 — "production ready" is a conjunction, not an average.

The readiness conversation usually ends with a percentage. "We're at 87%."
That number is an average over things that are not commensurable: a missing
dashboard and a missing kill switch both move it by a point, and the second
one is the whole difference between a bad week and an empty account.

So readiness here is an AND over conditions, each of which is individually
sufficient to say no:

  1. every **critical** registry entry is `verified` -- not partial, not
     planned, not waived;
  2. no critical entry is an accepted gap (the registry loader already
     refuses this, and it is re-checked here because the two rules are
     enforced in different places and only one of them is loaded at deploy
     time);
  3. every entry claiming a test names one that exists on disk;
  4. nothing is still `planned` for a phase that has already shipped;
  5. the traceability document is in sync with the registry.

Exit codes: 0 ready, 1 not ready, 2 could not evaluate. The third is
deliberately distinct -- a checker that cannot run has not said yes, and
collapsing it into "not ready" hides the difference between a failing gate
and a broken one.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Imported lazily inside main() so that --help works in an environment where
# the package is not importable, which is exactly when somebody is debugging
# why this will not run.


@dataclass(frozen=True)
class Blocker:
    condition: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.condition}] {self.detail}"


def check_critical_entries_are_verified(registry) -> list[Blocker]:
    out: list[Blocker] = []
    for entry in registry.entries:
        if entry.criticality != "critical":
            continue
        if entry.status != "verified":
            out.append(
                Blocker(
                    "critical-verified",
                    f"{entry.id} ({entry.title}) is {entry.status!r}, not 'verified'",
                )
            )
    return out


def check_no_critical_gap_is_accepted(registry) -> list[Blocker]:
    return [
        Blocker(
            "critical-not-waived",
            f"{entry.id} is critical and marked as an accepted gap",
        )
        for entry in registry.entries
        if entry.criticality == "critical" and entry.status == "accepted_gap"
    ]


def check_claimed_tests_exist(registry, root: Path) -> list[Blocker]:
    out: list[Blocker] = []
    for entry in registry.entries:
        for verification in entry.verification:
            if not (root / verification.test).exists():
                out.append(
                    Blocker(
                        "test-exists",
                        f"{entry.id} names {verification.test}, which is not on disk",
                    )
                )
    return out


def check_nothing_is_planned_for_a_shipped_phase(registry, shipped: set[str]) -> list[Blocker]:
    out: list[Blocker] = []
    for entry in registry.entries:
        if entry.status in {"planned", "partial"} and entry.planned_in in shipped:
            out.append(
                Blocker(
                    "phase-complete",
                    f"{entry.id} is still {entry.status!r} but {entry.planned_in} has shipped",
                )
            )
    return out


def check_traceability_is_in_sync(root: Path) -> list[Blocker]:
    """Runs the generator in --check mode, which is what CI does."""
    script = root / "scripts" / "generate_quality_docs.py"
    if not script.exists():
        return [Blocker("docs-in-sync", "the traceability generator is missing")]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,  # the return code is the answer, not an error
    )
    if result.returncode != 0:
        return [
            Blocker(
                "docs-in-sync",
                "REQUIREMENTS_TRACEABILITY.md is out of sync with the registry",
            )
        ]
    return []


def evaluate(root: Path, shipped: set[str]) -> list[Blocker]:
    # The repository root on sys.path, so this runs the same way from a
    # deploy step (`python3 scripts/check_production_readiness.py`) as it does
    # from the test suite. A checker that only works when invoked one
    # particular way is one that gets skipped in the other.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.quality.registry import load_registry

    registry = load_registry(root / "config" / "quality_registry.json")
    blockers: list[Blocker] = []
    blockers += check_critical_entries_are_verified(registry)
    blockers += check_no_critical_gap_is_accepted(registry)
    blockers += check_claimed_tests_exist(registry, root)
    blockers += check_nothing_is_planned_for_a_shipped_phase(registry, shipped)
    blockers += check_traceability_is_in_sync(root)
    return blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument(
        "--shipped",
        default="",
        help="comma-separated phases already merged (e.g. PR-001,PR-002). "
        "An entry still planned for one of these is a blocker.",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    shipped = {p.strip() for p in args.shipped.split(",") if p.strip()}

    try:
        blockers = evaluate(root, shipped)
    except Exception as exc:  # noqa: BLE001 - see exit code 2 in the docstring
        print(f"[ERROR] readiness could not be evaluated: {type(exc).__name__}: {exc}")
        return 2

    if blockers:
        print(f"[NOT READY] {len(blockers)} blocker(s):")
        for blocker in blockers:
            print(f"    {blocker}")
        # Deliberately no percentage. A number here would be read as progress
        # and progress is not the question.
        return 1

    print("[READY] every condition holds")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
