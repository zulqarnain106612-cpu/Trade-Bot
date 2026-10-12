#!/usr/bin/env python3
"""
PostToolUse policy stub.

Local tests, builds, lint, security scans, and quality gates are intentionally
not executed in an agent session. GitHub Actions is the sole validation path.
The hook remains registered for compatibility, but valid edit events are a
silent no-op. Malformed hook input fails open so an internal hook issue cannot
block unrelated work.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception as exc:  # malformed hook input is not the edit's fault
        print(f"[quality_gate] degraded, allowing: {exc}", file=sys.stderr)
    return


if __name__ == "__main__":
    main()
