#!/usr/bin/env python3
"""
Write the golden signal fixtures under tests/fixtures/signals/.

SIG-001. A golden fixture records what the deterministic part of the signal
path actually produced for a fixed market input: the data-quality verdict, the
feature vector at the decision bar, the risk-gate outcome and the final action.
CI then fails when any of them changes, so a refactor that alters what the bot
trades shows up as a diff in a reviewed file rather than as a change in the
PnL.

The fixtures are **generated, committed and reviewed**. That is the whole
mechanism: regenerating them makes the test pass again by construction, so the
protection comes from the diff being read, not from the test being green. A
regeneration whose diff nobody can explain is the finding.

Usage:
    python3 scripts/generate_signal_fixtures.py            # write
    python3 scripts/generate_signal_fixtures.py --check    # fail if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Anchor the repo root on __file__ inside the insert itself, so this runs from
# anywhere. tests/test_scripts_path_bootstrap.py enforces this shape.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from tests.signals.golden import CASES, build_record  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "signals"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any fixture on disk is stale instead of rewriting it",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for case in CASES:
        record = build_record(case)
        rendered = json.dumps(record, indent=2, sort_keys=True) + "\n"
        target = OUTPUT_DIR / f"{case.case_id}.json"

        if args.check:
            if not target.exists():
                stale.append(f"{target.name} is missing")
            elif target.read_text(encoding="utf-8") != rendered:
                stale.append(f"{target.name} is stale")
            continue

        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target.relative_to(PROJECT_ROOT)}")

    if args.check:
        if stale:
            print("\n".join(stale))
            print("Run: python3 scripts/generate_signal_fixtures.py -- then READ the diff.")
            return 1
        print(f"all {len(CASES)} signal fixtures are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
