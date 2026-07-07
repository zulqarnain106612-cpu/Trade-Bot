#!/usr/bin/env python3
"""Compact file-reading wrapper for agents.

This script is the preferred way to inspect source files. It never dumps raw code
by default and instead returns a compact summary that is safe for the context budget.
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from context_builder import summarize_source_file, truncate_to_budget


def build_summary(path: str | Path, query: str = "") -> str:
    target = Path(path)
    if not target.exists():
        return f"[missing] {target}"
    if target.is_dir():
        return f"[directory] {target} — use files inside the directory explicitly"

    try:
        return summarize_source_file(target, query=query, max_tokens=600)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return f"[summary-error] {target}: {exc}"


def summarize_paths(paths: list[str], query: str = "") -> str:
    summaries = [build_summary(path, query=query) for path in paths]
    combined = "\n\n".join(summaries)
    return truncate_to_budget(combined, 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact file reader for agent context budgets")
    parser.add_argument("paths", nargs="+", help="Files to summarize")
    parser.add_argument("--query", default="", help="Optional query to bias the summary")
    parser.add_argument("--raw", action="store_true", help="Print raw file contents instead of compact summary")
    args = parser.parse_args()

    if args.raw:
        for path in args.paths:
            target = Path(path)
            if not target.exists():
                print(f"[missing] {target}")
                continue
            print(f"===== {target} =====")
            print(target.read_text(errors="ignore"))
            print()
        return 0

    summary = summarize_paths(args.paths, query=args.query)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
