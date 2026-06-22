#!/usr/bin/env python3
"""
Output Router
==============
Intercepts agent output and routes it to the right destination.

DESTINATION 1 → Project files (.project-intel/):
  - Gaps, issues, bugs, missing features
  - Architecture decisions (ADRs)
  - Tasks and implementation plans
  - Diagnostic findings
  - Risk assessments
  - Broken/incomplete components
  - Security issues

DESTINATION 2 → Chat interface (stdout):
  - Conversational replies
  - Code implementations
  - Explanations and analysis
  - Confirmations and summaries

The agent is instructed (via system prompt) to wrap content in XML tags.
This router parses those tags and acts. If no tags present, all goes to chat.

Output format agents must use (injected via CONTEXT_PRIMER):
  <gap>...</gap>           → GAPS.md
  <issue>...</issue>       → ISSUES.md
  <decision>...</decision> → DECISION_LOG.md
  <task>...</task>         → OPEN_TASKS.md
  <risk>...</risk>         → RISK_LOG.md
  <diagnostic>...</diagnostic> → DIAGNOSTICS.md
  <broken>...</broken>     → BROKEN.md
  <missing>...</missing>   → MISSING.md
  <chat>...</chat>         → stdout (chat interface)
  Any untagged content     → stdout (chat interface)
"""

import json
import re
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


# ── Types ─────────────────────────────────────────────────────────────────────

class RoutedChunk(NamedTuple):
    tag: str          # "gap", "issue", "chat", "untagged", etc.
    content: str
    destination: str  # "project" or "chat"
    filename: str     # target file in .project-intel/ (if destination=project)


# ── Routing table ─────────────────────────────────────────────────────────────

ROUTING_TABLE = {
    # tag              filename              section header
    "gap":         ("GAPS.md",             "## Gap"),
    "issue":       ("ISSUES.md",           "## Issue"),
    "bug":         ("ISSUES.md",           "## Bug"),
    "broken":      ("BROKEN.md",           "## Broken Component"),
    "missing":     ("MISSING.md",          "## Missing Feature"),
    "decision":    ("DECISION_LOG.md",     "## Decision"),
    "adr":         ("DECISION_LOG.md",     "## ADR"),
    "task":        ("OPEN_TASKS.md",       "## Task"),
    "risk":        ("RISK_LOG.md",         "## Risk"),
    "diagnostic":  ("DIAGNOSTICS.md",      "## Diagnostic"),
    "security":    ("SECURITY_ISSUES.md",  "## Security Issue"),
    "performance": ("PERFORMANCE_LOG.md",  "## Performance Finding"),
    "todo":        ("OPEN_TASKS.md",       "## TODO"),
    "warning":     ("DIAGNOSTICS.md",      "## Warning"),
    "debt":        ("TECH_DEBT.md",        "## Tech Debt"),
}

# These tags go to chat (stdout) only
CHAT_TAGS = {"chat", "response", "reply", "code", "explanation", "summary"}


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_output(raw: str) -> list[RoutedChunk]:
    """
    Parse agent output into routed chunks.
    Handles:
      - <tag>content</tag> blocks
      - Untagged content (→ chat)
    """
    chunks: list[RoutedChunk] = []
    cursor = 0
    # Match any XML-style tag
    tag_pattern = re.compile(
        r'<([\w_-]+)(\s[^>]*)?>(.+?)</\1>',
        re.DOTALL | re.IGNORECASE
    )

    for match in tag_pattern.finditer(raw):
        # Capture untagged content before this match → chat
        before = raw[cursor:match.start()].strip()
        if before:
            chunks.append(RoutedChunk(
                tag="untagged",
                content=before,
                destination="chat",
                filename=""
            ))

        tag = match.group(1).lower()
        content = match.group(3).strip()
        cursor = match.end()

        if tag in CHAT_TAGS:
            chunks.append(RoutedChunk(
                tag=tag, content=content,
                destination="chat", filename=""
            ))
        elif tag in ROUTING_TABLE:
            fname, _ = ROUTING_TABLE[tag]
            chunks.append(RoutedChunk(
                tag=tag, content=content,
                destination="project", filename=fname
            ))
        else:
            # Unknown tag → chat (safe default)
            chunks.append(RoutedChunk(
                tag=tag, content=content,
                destination="chat", filename=""
            ))

    # Remaining untagged content → chat
    tail = raw[cursor:].strip()
    if tail:
        chunks.append(RoutedChunk(
            tag="untagged", content=tail,
            destination="chat", filename=""
        ))

    return chunks


# ── Writers ───────────────────────────────────────────────────────────────────

class ProjectFileWriter:
    """Appends structured content to .project-intel/ files."""

    def __init__(self, intel_dir: Path):
        self.intel_dir = intel_dir
        self._written_files: set[str] = set()
        self._counters: dict[str, int] = {}

    def write(self, chunk: RoutedChunk) -> str:
        """Append chunk to its target file. Returns the file path."""
        target = self.intel_dir / chunk.filename
        _, section_header = ROUTING_TABLE.get(chunk.tag, ("", "## Entry"))

        # Auto-increment counter for this file
        key = chunk.filename
        self._counters[key] = self._counters.get(key, 0) + 1
        count = self._counters[key]

        # Build entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (
            f"\n{section_header}-{count:03d} [{timestamp}]\n"
            f"{chunk.content}\n"
            f"{'─' * 60}\n"
        )

        # Create file with header if new
        if not target.exists():
            header = self._build_file_header(chunk.filename)
            target.write_text(header + entry)
        else:
            with target.open("a") as f:
                f.write(entry)

        self._written_files.add(chunk.filename)
        return str(target)

    def _build_file_header(self, filename: str) -> str:
        titles = {
            "GAPS.md":               "# Architecture Gaps",
            "ISSUES.md":             "# Issues & Bugs",
            "BROKEN.md":             "# Broken Components",
            "MISSING.md":            "# Missing Features",
            "DECISION_LOG.md":       "# Architecture Decision Log",
            "OPEN_TASKS.md":         "# Open Tasks",
            "RISK_LOG.md":           "# Risk Assessment Log",
            "DIAGNOSTICS.md":        "# Diagnostic Findings",
            "SECURITY_ISSUES.md":    "# Security Issues",
            "PERFORMANCE_LOG.md":    "# Performance Findings",
            "TECH_DEBT.md":          "# Technical Debt Register",
        }
        title = titles.get(filename, f"# {filename}")
        return (
            f"{title}\n"
            f"> Auto-maintained by Project Intelligence Router\n"
            f"> Agents: read this file for known issues before implementing\n\n"
        )

    def summary(self) -> dict[str, int]:
        return {f: self._counters.get(f, 0) for f in self._written_files}


# ── Session state updater ─────────────────────────────────────────────────────

def update_session_state(intel_dir: Path, chunks: list[RoutedChunk], writer: ProjectFileWriter):
    """Auto-update SESSION_STATE.json based on what was routed."""
    state_file = intel_dir / "SESSION_STATE.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except Exception:
        state = {}

    state["last_output_routed"] = datetime.now().isoformat()
    state["session_status"] = "output_delivered"

    # Track what types were written
    project_chunks = [c for c in chunks if c.destination == "project"]
    chat_chunks    = [c for c in chunks if c.destination == "chat"]

    if project_chunks:
        state["last_project_writes"] = list({c.filename for c in project_chunks})
        state["last_project_write_time"] = datetime.now().isoformat()

    # Extract gap/task IDs for quick reference
    gaps = [c.content[:80] for c in chunks if c.tag == "gap"]
    tasks = [c.content[:80] for c in chunks if c.tag in ("task", "todo")]
    if gaps:
        state["recent_gaps_found"] = gaps
    if tasks:
        state.setdefault("pending_tasks", []).extend(tasks)

    state_file.write_text(json.dumps(state, indent=2))


# ── Main router ───────────────────────────────────────────────────────────────

def route(raw_output: str, intel_dir: Path,
          verbose: bool = True) -> tuple[str, dict]:
    """
    Route agent output. Returns:
      - chat_output: string to print to stdout
      - routing_summary: dict of what went where
    """
    chunks = parse_output(raw_output)
    writer = ProjectFileWriter(intel_dir)

    chat_parts = []
    project_writes = []

    for chunk in chunks:
        if chunk.destination == "project":
            path = writer.write(chunk)
            project_writes.append((chunk.tag, chunk.filename, chunk.content[:60]))
        else:
            chat_parts.append(chunk.content)

    # Update session state
    update_session_state(intel_dir, chunks, writer)

    # Build chat output
    chat_output = "\n\n".join(p for p in chat_parts if p.strip())

    # Append routing notice to chat output (so user knows what was filed)
    if project_writes and verbose:
        notice_lines = ["\n\n---", "**📁 Auto-filed to project:**"]
        seen_files = {}
        for tag, fname, preview in project_writes:
            seen_files.setdefault(fname, []).append(f"`{tag}`: {preview}...")
        for fname, items in seen_files.items():
            notice_lines.append(f"- `.project-intel/{fname}` ({len(items)} entries)")
        notice_lines.append("*Agents in future sessions will read these automatically.*")
        chat_output += "\n".join(notice_lines)

    summary = {
        "total_chunks": len(chunks),
        "chat_chunks": len(chat_parts),
        "project_writes": project_writes,
        "files_written": list(writer.summary().keys()),
    }

    return chat_output, summary


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main():
    """
    Usage:
      agent-output | intel-route         # pipe agent output through router
      intel-route --file output.txt      # route from file
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Project root")
    parser.add_argument("--file",    help="Read input from file instead of stdin")
    parser.add_argument("--quiet",   action="store_true", help="No routing notice in chat")
    args = parser.parse_args()

    # Find project root
    sys.path.insert(0, str(Path(__file__).parent))
    from auto_prompt import find_project_root
    project_root = Path(args.project) if args.project else find_project_root()

    if not project_root:
        # No project — just pass through
        raw = open(args.file).read() if args.file else sys.stdin.read()
        sys.stdout.write(raw)
        return

    intel_dir = project_root / ".project-intel"
    intel_dir.mkdir(exist_ok=True)

    raw = open(args.file).read() if args.file else sys.stdin.read()
    chat_output, summary = route(raw, intel_dir, verbose=not args.quiet)

    sys.stdout.write(chat_output)
    if summary["project_writes"]:
        sys.stderr.write(
            f"\n[router] Filed {len(summary['project_writes'])} items to "
            f"{len(summary['files_written'])} project files\n"
        )


if __name__ == "__main__":
    main()
