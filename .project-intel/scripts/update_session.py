#!/usr/bin/env python3
"""
Session State Updater
======================
Agents run this at the END of every session to persist what they did.
Next session reads SESSION_STATE.json and DECISION_LOG.md — no code re-reading needed.

Usage:
  python update_session.py /path/to/project \
    --completed "TASK-002: HMM entropy gate implemented in detector.py" \
    --modified "src/regime/detector.py" \
    --decision "ADR-007: Entropy threshold set at 0.8 nats based on empirical regime transition analysis" \
    --next "TASK-001: Implement slippage model in src/risk/slippage.py" \
    --focus "slippage_model"
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", help="Path to project root")
    parser.add_argument("--completed", nargs="*", default=[], help="Tasks completed this session")
    parser.add_argument("--modified", nargs="*", default=[], help="Files modified this session")
    parser.add_argument("--decision", nargs="*", default=[], help="New architecture decisions made")
    parser.add_argument("--next", help="Next recommended task")
    parser.add_argument("--focus", help="Current focus area")
    parser.add_argument("--note", help="Any free-form note for next session")
    args = parser.parse_args()

    intel_dir = Path(args.project) / ".project-intel"
    if not intel_dir.exists():
        print(f"Error: {intel_dir} not found. Run extract_intelligence.py first.")
        sys.exit(1)

    # ── Update SESSION_STATE.json ──────────────────────────────────────────
    state_file = intel_dir / "SESSION_STATE.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    # Update implementation status for completed tasks
    for task in args.completed:
        # Extract the file/component from the task description
        task_lower = task.lower()
        if "entropy" in task_lower or "hmm" in task_lower:
            state.setdefault("implementation_status", {})["entropy_gate"] = f"COMPLETE — {task}"
        elif "slippage" in task_lower:
            state.setdefault("implementation_status", {})["slippage_model"] = f"COMPLETE — {task}"
        elif "order_fsm" in task_lower or "fsm" in task_lower:
            state.setdefault("implementation_status", {})["order_fsm"] = f"COMPLETE — {task}"
        elif "drift" in task_lower or "degradation" in task_lower:
            state.setdefault("implementation_status", {})["performance_drift_trigger"] = f"COMPLETE — {task}"
        elif "correlation" in task_lower:
            state.setdefault("implementation_status", {})["portfolio_correlation_layer"] = f"COMPLETE — {task}"

    state["last_updated"] = datetime.now().isoformat()
    state["last_session_completed"] = args.completed
    state["last_files_modified"] = args.modified

    if args.next:
        state["next_recommended_task"] = args.next
    if args.focus:
        state["current_focus"] = args.focus
    if args.note:
        state["session_note"] = args.note

    state_file.write_text(json.dumps(state, indent=2))
    print("✓ SESSION_STATE.json updated")

    # ── Append to DECISION_LOG.md ──────────────────────────────────────────
    if args.decision:
        dl_file = intel_dir / "DECISION_LOG.md"
        existing = dl_file.read_text() if dl_file.exists() else ""
        additions = []
        for i, decision in enumerate(args.decision):
            # Auto-increment ADR number
            existing_adrs = [int(x) for x in __import__("re").findall(r"ADR-(\d+)", existing)]
            next_num = max(existing_adrs) + 1 + i if existing_adrs else 7 + i
            additions.append(
                f"\n## ADR-{next_num:03d}: {decision}\n"
                f"**Date**: {datetime.now().strftime('%Y-%m-%d')}\n"
                f"**Session note**: {args.note or 'See SESSION_STATE.json'}\n"
            )
        dl_file.write_text(existing + "\n".join(additions))
        print(f"✓ DECISION_LOG.md updated with {len(args.decision)} new decisions")

    # ── Print next-session bootstrap ───────────────────────────────────────
    print("\n── Next Session Bootstrap ──────────────────────────────────────")
    print("Tell your agent exactly this:\n")
    print('"""')
    print("Use .project-intel/scripts/resume.py for the initial compact brief.")
    print("Use .project-intel/scripts/smart_read.py <path> for file-specific context.")
    print("Do not read large source files for orientation.")
    print(f"Current focus: {args.focus or state.get('current_focus', 'not set')}")
    print(f"Next task: {args.next or state.get('next_recommended_task', 'check OPEN_TASKS.md')}")
    if args.modified:
        print(f"Files changed last session: {', '.join(args.modified)}")
    print("Do NOT read any source files until you have a specific file to modify.")
    print('"""')


if __name__ == "__main__":
    main()
