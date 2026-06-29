#!/usr/bin/env python3
"""
SESSION RESUME — single command, zero file-reads for Claude.

Usage:
  python3 .project-intel/scripts/resume.py /path/to/project

Prints a compressed brief (~150 tokens) covering EVERYTHING Claude
needs to resume.  Claude reads this ONE output, then works.
No CONTEXT_PRIMER read. No SESSION_STATE read. No DECISION_LOG read.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def git_status(root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "status", "--short"], cwd=root, text=True, timeout=5
        ).strip()
        return out if out else "clean"
    except Exception:
        return "unknown"


def git_log(root: Path, n: int = 3) -> str:
    try:
        return subprocess.check_output(
            ["git", "log", f"-{n}", "--oneline"], cwd=root, text=True, timeout=5
        ).strip()
    except Exception:
        return ""


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    intel = root / ".project-intel"

    state: dict = {}
    try:
        state = json.loads((intel / "SESSION_STATE.json").read_text())
    except Exception:
        pass

    handoff: dict = state.get("handoff", {})
    impl: dict = state.get("implementation_status", {})

    # ── Compute in-progress and pending items ─────────────────────────────
    pending_decisions = state.get("open_decisions", [])
    next_task = handoff.get("next_step") or state.get("next_recommended_task", "check OPEN_TASKS.md")
    interruption = handoff.get("interruption_reason", "")
    files_touched = handoff.get("files_touched", [])
    last_checkpoint = handoff.get("last_checkpoint", "")

    # ── Git state ─────────────────────────────────────────────────────────
    status = git_status(root)
    recent_commits = git_log(root, 3)

    # ── Coverage ─────────────────────────────────────────────────────────
    coverage = state.get("coverage_pct", "?")
    total_sessions = state.get("total_sessions", "?")
    health = state.get("project_health", "")

    # ── Format brief ─────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        f"║  TRADE-BOT SESSION RESUME  [{now}]  Session #{total_sessions}",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
        f"▶ NEXT TASK : {next_task}",
        "",
    ]

    if interruption:
        lines += [f"⚠ INTERRUPTED : {interruption}"]
    if files_touched:
        lines += [f"  Files in-flight : {', '.join(files_touched)}"]
    if last_checkpoint:
        lines += [f"  Last checkpoint : {last_checkpoint}"]

    lines += [
        "",
        f"GIT STATUS : {status}",
    ]
    if recent_commits:
        lines += ["RECENT COMMITS:"] + [f"  {l}" for l in recent_commits.splitlines()]

    lines += [
        "",
        f"PROJECT HEALTH : {health}",
        f"COVERAGE       : {coverage}%  (gate=60%)",
        "",
        "IMPLEMENTATION STATUS:",
    ]
    for k, v in impl.items():
        short = v[:80] + "…" if len(v) > 80 else v
        lines.append(f"  {k:<40} {short}")

    if pending_decisions:
        lines += ["", "OPEN DECISIONS (do NOT re-debate, check DECISION_LOG.md):"]
        for d in pending_decisions[:5]:
            lines.append(f"  • {d[:100]}")

    lines += [
        "",
        "MODULE MAP (src/):",
        "  config.py  data/fetcher.py  data/storage.py  features/pipeline.py",
        "  regime/detector.py  models/trainer.py  risk/kelly.py  risk/gates.py",
        "  execution/paper.py  execution/live.py  strategies/filters.py",
        "  engine/signal_engine.py  engine/orchestrator.py  api/main.py",
        "",
        "SIGNAL FLOW: Exchange→fetch→store→features→regime→models→filters→sizing→gates→executor→api",
        "RISK GATES : DD>2% | losses>=3 | regime=volatile | pos>5% | paper<30d | live_gate_fail",
        "KEY CONSTS : KELLY×0.5 ceil 0.25 | PAPER_MIN 30d | LIVE_SHARPE 1.5 | LIVE_DD 15%",
        "",
        "OUTPUT ROUTING (mandatory — wrap every output):",
        "  <gap>  <issue>  <broken>  <missing>  <decision>  <task>  <risk>  <debt>  <chat>",
        "",
        "RULES:",
        "  1. Do NOT read source files until you are about to edit one.",
        "  2. Use MODULE_MAP.json for structure questions.",
        "  3. Run handoff checkpoint after every meaningful step.",
        "  4. Never re-read files from previous session — this brief IS the context.",
        "",
        "START COMMAND (register this session):",
        f'  python3 .project-intel/scripts/handoff.py start --agent claude --task "{next_task}"',
    ]

    print("\n".join(lines))

    # ── Auto-register session start ───────────────────────────────────────
    try:
        state["total_sessions"] = int(total_sessions or 0) + 1
        state["last_session_start"] = datetime.now().isoformat()
        state["session_status"] = "active"
        (intel / "SESSION_STATE.json").write_text(json.dumps(state, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
