#!/usr/bin/env python3
"""
scripts/vulner_fix_append.py — Append a new finding to Vulner-Fix.md.

Used by CI workflows, Claude, and Copilot agents to write findings
without ever overwriting existing content.

Usage:
    python scripts/vulner_fix_append.py \\
        --severity HIGH \\
        --tool bandit \\
        --file "src/api/main.py:42" \\
        --summary "Hardcoded credential detected" \\
        --fix "Use get_settings().api_secret_key instead of os.environ" \\
        --status Open

    python scripts/vulner_fix_append.py --mark-applied VF-005

Rules enforced:
    - Findings are APPENDED only — never inserted, never overwrite
    - IDs are auto-incremented from the last VF-NNN entry
    - --mark-applied changes only the Status line of that entry
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VULNER_FILE = Path(__file__).parent.parent / "Vulner-Fix.md"
MARKER = "<!-- NEW FINDINGS BELOW THIS LINE -->"


def next_id(content: str) -> str:
    """Return next VF-NNN id based on existing entries."""
    ids = re.findall(r"\[VF-(\d+)\]", content)
    if not ids:
        return "VF-001"
    return f"VF-{int(max(ids, key=int)) + 1:03d}"


def append_finding(
    severity: str,
    tool: str,
    file_loc: str,
    summary: str,
    fix: str,
    status: str = "Open",
) -> str:
    """Append a new finding block and return the assigned ID."""
    content = VULNER_FILE.read_text(encoding="utf-8")
    if MARKER not in content:
        content += f"\n{MARKER}\n"

    vid = next_id(content)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    block = f"""
### [{vid}] — {ts}
- **Severity:** {severity.upper()}
- **Tool:** {tool}
- **File:** `{file_loc}`
- **Status:** {status}
- **Summary:** {summary}
- **Fix:** {fix}
"""

    # Append after marker
    new_content = content.replace(MARKER, f"{MARKER}{block}")
    VULNER_FILE.write_text(new_content, encoding="utf-8")
    print(f"✓ Appended {vid} ({severity}) — {summary[:60]}")
    return vid


def mark_applied(vid: str) -> None:
    """Change Status of an existing entry to Applied."""
    content = VULNER_FILE.read_text(encoding="utf-8")
    # Find the entry block and update its Status line
    pattern = rf"(\[{re.escape(vid)}\].*?\n(?:.*\n)*?- \*\*Status:\*\* )(\w[\w ]*)"
    match = re.search(pattern, content)
    if not match:
        print(f"✗ Entry {vid} not found in Vulner-Fix.md", file=sys.stderr)
        sys.exit(1)
    new_content = content[:match.start(2)] + "Applied" + content[match.end(2):]
    VULNER_FILE.write_text(new_content, encoding="utf-8")
    print(f"✓ Marked {vid} as Applied")


def main() -> None:
    parser = argparse.ArgumentParser(description="Append finding to Vulner-Fix.md")
    parser.add_argument("--severity", default="MEDIUM")
    parser.add_argument("--tool", default="manual")
    parser.add_argument("--file", default="unknown")
    parser.add_argument("--summary", default="")
    parser.add_argument("--fix", default="")
    parser.add_argument("--status", default="Open")
    parser.add_argument("--mark-applied", metavar="VF_ID",
                        help="Mark an existing entry as Applied")
    args = parser.parse_args()

    if args.mark_applied:
        mark_applied(args.mark_applied)
    else:
        if not args.summary:
            parser.error("--summary is required")
        append_finding(
            severity=args.severity,
            tool=args.tool,
            file_loc=args.file,
            summary=args.summary,
            fix=args.fix,
            status=args.status,
        )


if __name__ == "__main__":
    main()
