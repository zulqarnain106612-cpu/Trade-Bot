#!/usr/bin/env python3
"""
SESSION RESUME — single command, zero follow-up file reads.

Outputs ONE compressed brief (~150-200 lines) containing:
  - Next task
  - Git state
  - Project health / coverage
  - Open decisions
  - FULL slim module map (src/ only, 1-line per file)
  - Signal flow + risk constants
  - Output routing rules
  - Exact rules forbidding further file reads

Claude reads this ONE output and begins work immediately.
No CONTEXT_PRIMER, SESSION_STATE, MODULE_MAP, DECISION_LOG, HANDOFF reads needed.
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
        return out[:400] if out else "clean"
    except Exception:
        return "unknown"


def git_log(root: Path, n: int = 3) -> str:
    try:
        return subprocess.check_output(
            ["git", "log", f"-{n}", "--oneline"], cwd=root, text=True, timeout=5
        ).strip()
    except Exception:
        return ""


def load_slim_map(intel: Path) -> dict:
    slim_path = intel / "MODULE_MAP_SLIM.json"
    if slim_path.exists():
        try:
            return json.loads(slim_path.read_text())
        except Exception:
            pass
    # Fallback: build from full if slim missing
    full_path = intel / "MODULE_MAP.json"
    if full_path.exists():
        try:
            full = json.loads(full_path.read_text())
            slim = {}
            for fp, info in full.items():
                if not fp.startswith("src/"):
                    continue
                if isinstance(info, dict):
                    purpose = info.get("purpose", "").strip().split("\n")[0][:90]
                    slim[fp] = {"purpose": purpose, "functions": info.get("functions", [])[:4]}
                else:
                    slim[fp] = {"purpose": str(info)[:90], "functions": []}
            # Save for next time
            slim_path.write_text(json.dumps(slim, indent=2))
            return slim
        except Exception:
            pass
    return {}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    intel = root / ".project-intel"

    # Load state
    state: dict = {}
    try:
        state = json.loads((intel / "SESSION_STATE.json").read_text())
    except Exception:
        pass

    handoff: dict = state.get("handoff", {})
    impl: dict = state.get("implementation_status", {})
    pending_decisions = state.get("open_decisions", [])
    next_task = (
        handoff.get("next_step")
        or state.get("next_recommended_task", "check OPEN_TASKS.md")
    )
    interruption = handoff.get("interruption_reason", "")
    files_touched = handoff.get("files_touched", [])
    last_checkpoint = handoff.get("last_checkpoint", "")
    coverage = state.get("coverage_pct", "?")
    total_sessions = state.get("total_sessions", 0)
    health = state.get("project_health", "")

    # Git
    status = git_status(root)
    recent_commits = git_log(root, 3)

    # Slim module map
    slim_map = load_slim_map(intel)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        f"║  TRADE-BOT RESUME  [{now}]  Session #{total_sessions}",
        "╚══════════════════════════════════════════════════════════════════╝",
        "",
        f"▶ NEXT TASK  : {next_task}",
    ]
    if interruption:
        lines += [f"⚠ INTERRUPTED: {interruption}"]
    if files_touched:
        lines += [f"  In-flight  : {', '.join(files_touched[:6])}"]
    if last_checkpoint:
        lines += [f"  Checkpoint : {last_checkpoint}"]

    lines += ["", f"PROJECT HEALTH : {health}", f"COVERAGE       : {coverage}% (gate=60%)"]

    lines += ["", "GIT STATUS:"]
    for l in status.splitlines()[:12]:
        lines.append(f"  {l}")
    if recent_commits:
        lines += ["RECENT COMMITS:"]
        for l in recent_commits.splitlines():
            lines.append(f"  {l}")

    # Implementation status — compact
    if impl:
        lines += ["", "IMPLEMENTATION STATUS (key areas):"]
        for k, v in list(impl.items())[:12]:
            short = str(v)[:70] + ("…" if len(str(v)) > 70 else "")
            lines.append(f"  {k:<38} {short}")

    # Open decisions — top 5
    if pending_decisions:
        lines += ["", "OPEN DECISIONS (do NOT re-debate — check DECISION_LOG.md for context):"]
        for d in pending_decisions[:5]:
            lines.append(f"  • {str(d)[:110]}")

    # Full slim module map
    if slim_map:
        lines += ["", "SOURCE MODULE MAP (src/ — full):"]
        for fp, info in slim_map.items():
            purpose = info.get("purpose", "") if isinstance(info, dict) else str(info)
            fns = info.get("functions", []) if isinstance(info, dict) else []
            fn_str = f"  [{', '.join(fns[:4])}]" if fns else ""
            lines.append(f"  {fp:<52} {purpose[:60]}{fn_str}")

    lines += [
        "",
        "SIGNAL FLOW:",
        "  Exchange→fetcher→storage→features→regime→models→filters→sizing→gates→executor→api",
        "",
        "RISK GATES (never bypass):",
        "  DD>2% | losses>=3 | regime=volatile | pos>5% | paper<30d | live_gate_fail",
        "  KELLY×0.5 ceil 0.25 | PAPER_MIN 30d | LIVE_SHARPE≥1.5 | LIVE_DD≤15%",
        "",
        "OUTPUT ROUTING (wrap every output in tags):",
        "  <gap>  <issue>  <broken>  <missing>  <decision>  <task>  <risk>  <debt>  <chat>",
        "",
        "══ RULES — READ BEFORE FIRST ACTION ══",
        "  1. This brief IS your complete context. Do NOT read any other files to orient.",
        "  2. Read a source file ONLY immediately before editing it — not for context.",
        "  3. Do NOT read MODULE_MAP.json — the map above replaces it entirely.",
        "  4. Do NOT read CONTEXT_PRIMER, SESSION_STATE, HANDOFF, DECISION_LOG.",
        "  5. Checkpoint after every meaningful change:",
        "     python3 .project-intel/scripts/handoff.py checkpoint --agent claude \\",
        '       --completed "done X" --next "do Y" --files "src/file.py"',
        "  6. Commit intel files after checkpointing: bash scripts/claude-commit.sh",
    ]

    print("\n".join(lines))

    # Auto-register session start in state
    try:
        state["total_sessions"] = int(total_sessions or 0) + 1
        state["last_session_start"] = datetime.now().isoformat()
        state["session_status"] = "active"
        (intel / "SESSION_STATE.json").write_text(json.dumps(state, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
