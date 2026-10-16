#!/usr/bin/env python3
"""Retired CI verdict endpoint.

Raw Action log/check retrieval is intentionally unavailable through this module.
All agent-facing diagnostics must use the centralized, SHA-bound report command.
The compatibility function returns the mandated denial so older pre-tool
integrations fail closed rather than silently switching back to raw CI access.
"""

from __future__ import annotations

import sys

DENIAL = (
    "THAPPAR — CI DIAGNOSTIC ACCESS BLOCKED. Use only: "
    "./scripts/prfail --pr <PR_NUMBER>. "
    "Direct or alternative diagnostic retrieval is prohibited."
)


class VerdictError(RuntimeError):
    """Compatibility exception for pre-existing hook callers."""


def has_credentials() -> bool:
    """Kept only for older hook integrations; it does not validate credentials."""
    return True


def verdict(pr_hint=None) -> str:
    """Older hook callers receive a denial, never raw or filtered run data."""
    del pr_hint
    return DENIAL


def main(argv: list[str] | None = None) -> int:
    del argv
    sys.stderr.write(DENIAL + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
