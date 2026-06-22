#!/usr/bin/env python3
"""
Output Monitor
===============
Watches for agent output and automatically updates SESSION_STATE.json.

Works with:
- Claude Code CLI (watches ~/.claude/projects/ for new responses)
- Any agent that writes to stdout/file
- VSCode Copilot (watches .copilot-output if configured)

When output is detected:
1. Parses the output for file mentions (auto-detects modified files)
2. Parses for task completion signals
3. Updates SESSION_STATE.json automatically
4. No manual update_session.py needed
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path


# ── Patterns to detect in agent output ───────────────────────────────────────

# Files being modified
FILE_PATTERNS = [
    r"(?:writing|modifying|editing|creating|updated?)\s+[`'\"]?(src/[\w/]+\.py)[`'\"]?",
    r"(?:writing|modifying|editing|creating|updated?)\s+[`'\"]?([\w/]+\.py)[`'\"]?",
    r"```python\s*#\s*([\w/]+\.py)",
    r"save[ds]?\s+to\s+[`'\"]?([\w/\.]+)[`'\"]?",
]

# Task completion signals
COMPLETION_PATTERNS = [
    r"(?:implemented|completed|done|finished)\s+(.{10,80})",
    r"TASK-\d+\s+(?:complete|done|implemented)",
    r"(?:GAP-\d+)\s+(?:resolved|fixed|addressed)",
]

# Decision signals
DECISION_PATTERNS = [
    r"(?:decided|choosing|chose|decision):\s*(.{10,120})",
    r"ADR-\d+",
]

# Next task signals
NEXT_PATTERNS = [
    r"next(?:\s+step)?:\s*(.{10,120})",
    r"(?:should|recommend)\s+(?:next|then)\s+(.{10,120})",
    r"TASK-(\d+).*?(?:next|recommend|suggest)",
]


def parse_agent_output(text: str) -> dict:
    """Extract structured info from agent output text."""
    result = {
        "files_modified": [],
        "tasks_completed": [],
        "decisions": [],
        "next_task": None,
        "raw_preview": text[:200],
    }

    for pattern in FILE_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        result["files_modified"].extend(matches)

    for pattern in COMPLETION_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        result["tasks_completed"].extend(matches)

    for pattern in DECISION_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        result["decisions"].extend(matches)

    for pattern in NEXT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["next_task"] = match.group(1).strip()[:120]
            break

    # Deduplicate
    result["files_modified"] = list(dict.fromkeys(result["files_modified"]))
    result["tasks_completed"] = list(dict.fromkeys(result["tasks_completed"]))

    return result


def update_state(project_root: Path, parsed: dict):
    """Write parsed output info into SESSION_STATE.json."""
    state_file = project_root / ".project-intel" / "SESSION_STATE.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except Exception:
        state = {}

    state["last_output_received"] = datetime.now().isoformat()
    state["session_status"] = "output_delivered"

    if parsed["files_modified"]:
        state["last_files_modified"] = parsed["files_modified"]

    if parsed["tasks_completed"]:
        state["last_session_completed"] = parsed["tasks_completed"]
        # Update implementation_status
        impl = state.setdefault("implementation_status", {})
        for task in parsed["tasks_completed"]:
            task_l = task.lower()
            if "entropy" in task_l or "hmm" in task_l:
                impl["entropy_gate"] = f"COMPLETE — {task[:60]}"
            elif "slippage" in task_l:
                impl["slippage_model"] = f"COMPLETE — {task[:60]}"
            elif "fsm" in task_l or "order" in task_l:
                impl["order_fsm"] = f"COMPLETE — {task[:60]}"
            elif "drift" in task_l or "degradation" in task_l:
                impl["performance_drift_trigger"] = f"COMPLETE — {task[:60]}"

    if parsed["next_task"]:
        state["next_recommended_task"] = parsed["next_task"]

    if parsed["decisions"]:
        state["last_decisions"] = parsed["decisions"]

    state_file.write_text(json.dumps(state, indent=2))
    return state


def watch_output_file(project_root: Path, output_file: Path):
    """Watch a file for agent output — update state when content changes."""
    print(f"Watching for agent output: {output_file}")
    last_size = 0
    last_mtime = 0.0

    while True:
        try:
            if output_file.exists():
                stat = output_file.stat()
                if stat.st_mtime > last_mtime or stat.st_size != last_size:
                    last_mtime = stat.st_mtime
                    last_size = stat.st_size

                    content = output_file.read_text(errors="ignore")
                    parsed = parse_agent_output(content)
                    state = update_state(project_root, parsed)

                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Output detected, state updated")
                    if parsed["files_modified"]:
                        print(f"  Files: {parsed['files_modified']}")
                    if parsed["tasks_completed"]:
                        print(f"  Completed: {parsed['tasks_completed'][:2]}")
                    if parsed["next_task"]:
                        print(f"  Next: {parsed['next_task'][:60]}")

        except KeyboardInterrupt:
            print("Output monitor stopped")
            break
        except Exception as e:
            print(f"Monitor error: {e}")

        time.sleep(1.0)


def process_piped_output(project_root: Path):
    """
    Read agent output from stdin, parse it, update state, pass through to stdout.
    Usage: claude "task" | intel-capture
    """
    content = sys.stdin.read()
    parsed = parse_agent_output(content)
    update_state(project_root, parsed)
    # Pass through so output still appears in terminal
    sys.stdout.write(content)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Project root (auto-detected if not set)")
    parser.add_argument("--watch", help="File to watch for agent output")
    parser.add_argument("--pipe", action="store_true",
                        help="Read from stdin, parse, update state, pass through")
    args = parser.parse_args()

    from auto_prompt import find_project_root  # reuse finder
    project_root = Path(args.project) if args.project else find_project_root()
    if not project_root:
        if args.pipe:
            # No project — just pass through
            sys.stdout.write(sys.stdin.read())
        return

    if args.pipe:
        process_piped_output(project_root)
    elif args.watch:
        watch_output_file(project_root, Path(args.watch))
    else:
        print("Usage: --pipe (stdin passthrough) or --watch FILE")


if __name__ == "__main__":
    main()
