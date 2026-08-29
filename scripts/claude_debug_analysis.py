#!/usr/bin/env python3
"""
scripts/claude_debug_analysis.py — Called by auto-debug.yml GitHub Action.

Reads ci_failures.json, sends to Claude API, writes analysis to ai_analysis.md,
appends findings to Vulner-Fix.md.

Usage: python scripts/claude_debug_analysis.py
Env:   ANTHROPIC_API_KEY must be set
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent


def main() -> None:
    failures_path = ROOT / "ci_failures.json"
    if not failures_path.exists():
        print("No ci_failures.json found.")
        return

    failures: dict = json.loads(failures_path.read_text())
    if not failures:
        print("No failures to analyse.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — skipping AI analysis.")
        _write_failures_only(failures)
        return

    try:
        import anthropic
    except ImportError:
        print("anthropic not installed — pip install anthropic")
        _write_failures_only(failures)
        return

    # Build failure summary
    failure_text = ""
    for tool, data in failures.items():
        failure_text += f"\n\n### {tool} (exit {data['returncode']})\n"
        failure_text += (data.get("stdout") or "")[:2000]

    # Get changed files context
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout.strip().split("\n")
    except Exception:
        changed = []

    src_context = ""
    for f in changed[:5]:
        fpath = ROOT / f
        if f.endswith(".py") and fpath.exists():
            src_context += f"\n### {f}\n" + fpath.read_text()[:1200]

    prompt = (
        "You are a senior Python engineer reviewing CI failures for an async "
        "algorithmic trading bot (Python 3.11, FastAPI, asyncio, aiosqlite, xgboost, structlog).\n\n"
        "Analyse the failures below and provide:\n"
        "1. Root cause (1-2 sentences per tool)\n"
        "2. Exact fix (code diff or command)\n"
        "3. Confidence: HIGH / MEDIUM / LOW\n\n"
        "Rules: no f-string SQL, no time.sleep() in async, structlog only.\n\n"
        f"CI FAILURES:\n{failure_text}\n\n"
        f"CHANGED FILES:\n{src_context}\n\n"
        "Format: concise GitHub-flavoured Markdown. One section per failing tool."
    )

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = msg.content[0].text

    ai_path = ROOT / "ai_analysis.md"
    ai_path.write_text(f"## AI Debug Analysis\n\n{analysis}\n")
    print(f"AI analysis written to {ai_path}")

    # Write to Vulner-Fix.md
    _write_failures_only(failures)


def _write_failures_only(failures: dict) -> None:
    for tool, data in failures.items():
        if data.get("returncode") == 0:
            continue
        first_line = (data.get("stdout") or "").strip().split("\n")[0][:120]
        subprocess.run(
            [
                sys.executable, "scripts/vulner_fix_append.py",
                "--severity", "HIGH",
                "--tool", tool,
                "--file", "see ci artifact",
                "--summary", f"CI failure [{tool}]: {first_line}",
                "--fix", "See ai_analysis.md in workflow artifacts for Claude-generated fix",
                "--status", "Open",
            ],
            cwd=ROOT,
            check=False,
        )


if __name__ == "__main__":
    main()
