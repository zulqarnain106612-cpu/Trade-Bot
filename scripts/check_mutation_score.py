#!/usr/bin/env python3
"""
Run mutation testing per subsystem and enforce the declared floors.

GOV-003, RISK-006, EXEC-007, SIG-003.

The reason this exists, in the source document's words:

> A high coverage number with a poor mutation score is weak evidence.

Coverage says a line ran. A mutation score says the suite would have noticed
if the line had been wrong. This repository has a 99% coverage gate and, until
this script, no answer at all to the second question.

Usage:
    python3 scripts/check_mutation_score.py                 # every subsystem
    python3 scripts/check_mutation_score.py --subsystem risk
    python3 scripts/check_mutation_score.py --report out.json
    python3 scripts/check_mutation_score.py --dry-run       # plan only

Requires `mutmut` on PATH. It is deliberately **not** in requirements-dev:
a mutation run is minutes of CPU per module, so it belongs in the nightly
workflow that installs it, not in the dependency set every contributor and
every pull-request job installs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = PROJECT_ROOT / "config" / "mutation_thresholds.json"

#: mutmut's summary line, e.g. "killed: 120  survived: 8  timeout: 1".
_COUNT_RE = re.compile(r"(?P<name>killed|survived|timeout|suspicious|skipped)\s*:?\s*(?P<n>\d+)")


@dataclass(frozen=True)
class MutationResult:
    subsystem: str
    killed: int
    survived: int
    timeout: int
    min_score: float
    requirement: str

    @property
    def total(self) -> int:
        # A timeout is a mutant the suite did not kill within the budget.
        # Counting it as killed would let a slow test suite buy a score.
        return self.killed + self.survived + self.timeout

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        # A run that generated no mutants has proved nothing. Treating it as
        # a pass is how this check goes green after someone points it at a
        # path that no longer exists.
        return self.total > 0 and self.score >= self.min_score

    def line(self) -> str:
        status = "ok" if self.passed else "FAIL"
        return (
            f"[{status:4}] {self.subsystem:<14} {self.score:6.1%} "
            f"(floor {self.min_score:.0%}, {self.killed} killed / {self.total} mutants, "
            f"{self.requirement})"
        )


def load_thresholds(path: Path = THRESHOLDS) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse_counts(output: str) -> dict[str, int]:
    """Pull the killed/survived/timeout counts out of a mutmut results run."""
    counts = {"killed": 0, "survived": 0, "timeout": 0}
    for match in _COUNT_RE.finditer(output.lower()):
        name = match.group("name")
        if name in counts:
            counts[name] = int(match.group("n"))
    return counts


def run_subsystem(name: str, spec: dict, timeout_s: int) -> MutationResult:
    """Mutate one subsystem's paths and collect its score."""
    paths = ",".join(spec["paths"])
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["mutmut", "run", "--paths-to-mutate", paths, "--no-progress"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    results = subprocess.run(  # noqa: S603
        ["mutmut", "results"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    counts = parse_counts(results.stdout)
    return MutationResult(
        subsystem=name,
        killed=counts["killed"],
        survived=counts["survived"],
        timeout=counts["timeout"],
        min_score=float(spec["min_score"]),
        requirement=spec.get("requirement", ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subsystem", help="run one subsystem instead of all")
    parser.add_argument("--report", type=Path, help="write the results as JSON")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be mutated and exit; does not require mutmut",
    )
    parser.add_argument("--timeout-s", type=int, default=3600)
    args = parser.parse_args()

    config = load_thresholds()
    subsystems = config["subsystems"]
    if args.subsystem:
        if args.subsystem not in subsystems:
            print(f"unknown subsystem {args.subsystem!r}; declared: {', '.join(subsystems)}")
            return 2
        subsystems = {args.subsystem: subsystems[args.subsystem]}

    missing = [
        path
        for spec in subsystems.values()
        for path in spec["paths"]
        if not (PROJECT_ROOT / path).exists()
    ]
    if missing:
        # A path that no longer exists generates no mutants, and a subsystem
        # with no mutants would otherwise score a silent 0/0.
        print(f"mutation targets missing from the tree: {', '.join(missing)}")
        return 2

    if args.dry_run:
        for name, spec in subsystems.items():
            print(f"{name}: floor {spec['min_score']:.0%} over {len(spec['paths'])} module(s)")
            for path in spec["paths"]:
                print(f"    {path}")
        return 0

    if not shutil.which("mutmut"):
        print(
            "mutmut is not on PATH. It is deliberately not in requirements-dev -- "
            "a mutation run is minutes of CPU per module. Install it in the job "
            "that needs it: pip install mutmut"
        )
        return 2

    results = [run_subsystem(name, spec, args.timeout_s) for name, spec in subsystems.items()]
    for result in results:
        print(result.line())

    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    r.subsystem: {
                        "score": round(r.score, 4),
                        "killed": r.killed,
                        "survived": r.survived,
                        "timeout": r.timeout,
                        "min_score": r.min_score,
                        "passed": r.passed,
                        "requirement": r.requirement,
                    }
                    for r in results
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"\n{len(failed)} subsystem(s) below the mutation floor")
        return 1
    print(f"\nall {len(results)} subsystem(s) clear their mutation floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
