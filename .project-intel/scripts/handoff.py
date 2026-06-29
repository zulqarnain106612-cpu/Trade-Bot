#!/usr/bin/env python3
"""
Agent Handoff Manager
======================
Tracks which agent is working, what it completed, and where it stopped.
Any agent — Claude, Copilot, Amazon Q, or a new session of the same agent —
reads HANDOFF.md to resume exactly where the previous agent left off.

Usage:
  # Agent starting a session:
  python3 .project-intel/scripts/handoff.py start --agent claude --task "implement entropy gate"

  # Agent checkpointing mid-work:
  python3 .project-intel/scripts/handoff.py checkpoint --agent claude \
    --completed "modified src/regime/detector.py — entropy computed" \
    --next "wire entropy scalar into signal_engine.py line 312" \
    --files "src/regime/detector.py"

  # Agent finishing cleanly:
  python3 .project-intel/scripts/handoff.py finish --agent claude \
    --completed "entropy gate fully implemented and tested" \
    --next "TASK-001: slippage model in src/risk/slippage.py"

  # Any agent reading current handoff state:
  python3 .project-intel/scripts/handoff.py status
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

INTEL_DIR    = Path(__file__).parent.parent
HANDOFF_FILE = INTEL_DIR / "HANDOFF.md"
STATE_FILE   = INTEL_DIR / "SESSION_STATE.json"
AGENTS       = {"claude", "copilot", "amazonq", "unknown"}


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        return {}


def _save_state(state: dict):
    state["last_updated"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── HANDOFF.md writer ─────────────────────────────────────────────────────────

def _write_handoff(state: dict):
    """
    Writes HANDOFF.md — the single file any agent reads to resume work.
    Structured so it fits in ~600 tokens.
    """
    hs    = state.get("handoff", {})
    agent = hs.get("active_agent", "none")
    status= hs.get("status", "idle")
    task  = hs.get("current_task", "not set")
    started = hs.get("session_started", "unknown")
    last_cp = hs.get("last_checkpoint", "none")
    completed = hs.get("completed_this_session", [])
    next_step = hs.get("next_step", "check OPEN_TASKS.md")
    files_touched = hs.get("files_touched", [])
    interruption  = hs.get("interruption_reason", "")
    history = hs.get("session_history", [])[-5:]  # last 5 sessions only

    status_icon = {
        "active":      "🟢 ACTIVE",
        "completed":   "✅ COMPLETED",
        "interrupted": "🔴 INTERRUPTED — resume required",
        "idle":        "⚪ IDLE",
    }.get(status, status.upper())

    lines = [
        "# Agent Handoff State",
        f"> Updated: {_now()} | Read this before starting any work.",
        "",
        "## Current Status",
        f"**Agent**:   {agent}",
        f"**Status**:  {status_icon}",
        f"**Task**:    {task}",
        f"**Started**: {started}",
        f"**Last checkpoint**: {last_cp}",
        "",
    ]

    if status == "interrupted":
        lines += [
            "## ⚠ INTERRUPTION — Resume from here",
            f"**Reason**: {interruption or 'session ended unexpectedly'}",
            "",
            "### What was completed before interruption:",
        ]
        for c in completed:
            lines.append(f"  - {c}")
        lines += [
            "",
            f"### Exact resume point:",
            f"  {next_step}",
            "",
            "### Files modified (may have uncommitted changes):",
        ]
        for f in files_touched:
            lines.append(f"  - {f}")
        lines += [
            "",
            "### Action required:",
            "  1. Run `git status` — check for uncommitted changes",
            "  2. Run `git diff` — review what was partially done",
            "  3. Read the files listed above — continue from next_step above",
            "  4. Do NOT restart from scratch — work is partially done",
            "",
        ]
    elif status == "active":
        lines += [
            "## ⚠ ANOTHER AGENT IS ACTIVE",
            f"If {agent} is no longer running, status is stale.",
            "Check: `git log --oneline -3` — if no recent commits, agent likely crashed.",
            "Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`",
            "",
        ]
    elif status == "completed":
        lines += [
            "## ✅ Last session completed cleanly",
            "",
            "### Completed:",
        ]
        for c in completed:
            lines.append(f"  - {c}")
        lines += [""]

    lines += [
        "## Next Step for Incoming Agent",
        f"  {next_step}",
        "",
        "## Files to Check",
    ]
    if files_touched:
        for f in files_touched:
            lines.append(f"  - {f}")
    else:
        lines.append("  (no specific files — start from OPEN_TASKS.md)")

    lines += [
        "",
        "## Session History (last 5)",
    ]
    for h in reversed(history):
        lines.append(
            f"  [{h.get('time','?')}] {h.get('agent','?')} — "
            f"{h.get('status','?')}: {h.get('summary','')[:80]}"
        )

    lines += [
        "",
        "## Quick Start for Any Agent",
        "```",
        "# 1. Read context (mandatory):",
        "cat .project-intel/CONTEXT_PRIMER.md",
        "cat .project-intel/HANDOFF.md          ← you are here",
        "cat .project-intel/SESSION_STATE.json",
        "",
        "# 2. Check uncommitted work:",
        "git status --short",
        "git diff --stat",
        "",
        "# 3. Register yourself:",
        "python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'describe task'",
        "",
        "# 4. Checkpoint as you work (every meaningful step):",
        "python3 .project-intel/scripts/handoff.py checkpoint --agent YOUR_AGENT \\",
        "  --completed 'what you just did' --next 'exact next action' --files 'src/x.py'",
        "```",
    ]

    HANDOFF_FILE.write_text("\n".join(lines))


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_start(agent: str, task: str):
    state = _load_state()
    hs    = state.setdefault("handoff", {})

    # Archive previous session if it existed
    history = hs.get("session_history", [])
    if hs.get("active_agent") and hs.get("status") in ("active", "interrupted"):
        history.append({
            "time":    _now(),
            "agent":   hs.get("active_agent", "unknown"),
            "status":  "interrupted" if hs.get("status") == "active" else hs.get("status"),
            "summary": hs.get("current_task", "")[:80],
        })

    hs.update({
        "active_agent":            agent,
        "status":                  "active",
        "current_task":            task,
        "session_started":         _now(),
        "last_checkpoint":         _now(),
        "completed_this_session":  [],
        "files_touched":           [],
        "next_step":               task,
        "interruption_reason":     "",
        "session_history":         history[-10:],  # keep last 10
    })

    _save_state(state)
    _write_handoff(state)
    print(f"✓ Handoff: {agent} started — task: {task}")


def cmd_checkpoint(agent: str, completed: str, next_step: str, files: str = ""):
    state = _load_state()
    hs    = state.setdefault("handoff", {})

    # Update active agent if different (takeover)
    if hs.get("active_agent") != agent:
        hs["active_agent"] = agent

    done = hs.get("completed_this_session", [])
    if completed:
        done.append(f"[{_now()}] {completed}")
    hs["completed_this_session"] = done[-20:]  # keep last 20

    if files:
        touched = hs.get("files_touched", [])
        for f in files.split(","):
            f = f.strip()
            if f and f not in touched:
                touched.append(f)
        hs["files_touched"] = touched

    hs["next_step"]       = next_step
    hs["last_checkpoint"] = _now()
    hs["status"]          = "active"

    _save_state(state)
    _write_handoff(state)
    print(f"✓ Checkpoint saved: next → {next_step[:60]}")


def cmd_finish(agent: str, completed: str, next_task: str):
    state = _load_state()
    hs    = state.setdefault("handoff", {})

    done = hs.get("completed_this_session", [])
    if completed:
        done.append(f"[{_now()}] {completed}")

    history = hs.get("session_history", [])
    history.append({
        "time":    _now(),
        "agent":   agent,
        "status":  "completed",
        "summary": completed[:80],
    })

    hs.update({
        "active_agent":           agent,
        "status":                 "completed",
        "completed_this_session": done[-20:],
        "next_step":              next_task,
        "last_checkpoint":        _now(),
        "session_history":        history[-10:],
    })

    # Also update top-level SESSION_STATE
    state["next_recommended_task"] = next_task
    state["current_focus"]         = f"completed by {agent} — {completed[:80]}"

    _save_state(state)
    _write_handoff(state)
    print(f"✓ Session finished cleanly. Next: {next_task[:60]}")


def cmd_interrupt(agent: str, reason: str, next_step: str, files: str = ""):
    """Called automatically by the daemon if it detects a crash or stale session."""
    state = _load_state()
    hs    = state.setdefault("handoff", {})

    if files:
        touched = hs.get("files_touched", [])
        for f in files.split(","):
            f = f.strip()
            if f and f not in touched:
                touched.append(f)
        hs["files_touched"] = touched

    history = hs.get("session_history", [])
    history.append({
        "time":   _now(),
        "agent":  agent or hs.get("active_agent", "unknown"),
        "status": "interrupted",
        "summary": reason[:80],
    })

    hs.update({
        "status":              "interrupted",
        "interruption_reason": reason,
        "next_step":           next_step,
        "last_checkpoint":     _now(),
        "session_history":     history[-10:],
    })

    _save_state(state)
    _write_handoff(state)
    print(f"⚠ Interruption recorded: {reason[:60]}")


def cmd_status():
    state = _load_state()
    hs    = state.get("handoff", {})
    if not hs:
        print("No handoff state found — no agent has registered yet.")
        return
    print(f"Agent:       {hs.get('active_agent','none')}")
    print(f"Status:      {hs.get('status','unknown')}")
    print(f"Task:        {hs.get('current_task','not set')}")
    print(f"Next step:   {hs.get('next_step','?')}")
    print(f"Checkpoint:  {hs.get('last_checkpoint','never')}")
    completed = hs.get("completed_this_session", [])
    if completed:
        print(f"Completed ({len(completed)}):")
        for c in completed[-3:]:
            print(f"  {c[:80]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Agent handoff manager")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("start");       s.add_argument("--agent", required=True); s.add_argument("--task", required=True)
    c = sub.add_parser("checkpoint");  c.add_argument("--agent", required=True); c.add_argument("--completed", default=""); c.add_argument("--next", required=True); c.add_argument("--files", default="")
    f = sub.add_parser("finish");      f.add_argument("--agent", required=True); f.add_argument("--completed", default=""); f.add_argument("--next", required=True)
    i = sub.add_parser("interrupt");   i.add_argument("--agent", default=""); i.add_argument("--reason", default="session ended"); i.add_argument("--next", default="check HANDOFF.md"); i.add_argument("--files", default="")
    sub.add_parser("status")

    args = p.parse_args()

    if args.cmd == "start":       cmd_start(args.agent, args.task)
    elif args.cmd == "checkpoint": cmd_checkpoint(args.agent, args.completed, args.next, args.files)
    elif args.cmd == "finish":     cmd_finish(args.agent, args.completed, args.next)
    elif args.cmd == "interrupt":  cmd_interrupt(args.agent, args.reason, args.next, args.files)
    elif args.cmd == "status":     cmd_status()
    else:                          p.print_help()


if __name__ == "__main__":
    main()
