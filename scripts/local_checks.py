#!/usr/bin/env python3
"""Retired CI-notice interface; it must never query GitHub or emit PR comments."""

from __future__ import annotations

import argparse
import sys

DENIAL = (
    "THAPPAR — CI DIAGNOSTIC ACCESS BLOCKED. Use only: "
    "./scripts/prfail --pr <PR_NUMBER>. "
    "Direct or alternative diagnostic retrieval is prohibited."
)
LOCAL_CHECKS_DISABLED = (
    "local check execution is disabled by repository policy; "
    "GitHub Actions is the sole test/build/check execution path"
)


def die(message: str) -> None:
    raise SystemExit(message)


def prepare() -> int:
    """Retired: never fetch the historical PR-comment diagnostic route."""
    die(DENIAL)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare", help="retired; use the canonical report-only command")
    sub.add_parser("run", help="disabled: checks execute only in GitHub Actions")
    args = parser.parse_args(argv)
    if args.action == "run":
        die(LOCAL_CHECKS_DISABLED)
    die(DENIAL)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

