#!/usr/bin/env python3
"""
SESSION RESUME — single command, complete context, zero follow-up reads.
Output target: ≤500 tokens. Never reads source files.
Session counter increments ONLY when --new-session flag passed (by shell_hook).
"""
import json, subprocess, sys
from datetime import datetime
from pathlib import Path


def git_status(root):
    try:
        out = subprocess.check_output(
            ["git", "status", "--short"], cwd=root, text=True, timeout=5
        ).strip()
        # Only show staged/modified, skip untracked (??) to cut noise
        lines = [l for l in out.splitlines() if not l.startswith("??")][:5]
        return "\n".join(lines) if lines else "clean"
    except Exception:
        return "unknown"


def git_log(root, n=2):
    try:
        return subprocess.check_output(
            ["git", "log", f"-{n}", "--oneline"], cwd=root, text=True, timeout=5
        ).strip()
    except Exception:
        return ""


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    # --new-session flag: only shell_hook passes this on real Claude Desktop launch
    new_session = "--new-session" in sys.argv

    intel = root / ".project-intel"

    state = {}
    try:
        state = json.loads((intel / "SESSION_STATE.json").read_text())
    except Exception:
        pass

    handoff  = state.get("handoff", {})
    impl     = state.get("implementation_status", {})
    decisions = state.get("open_decisions", [])
    next_task = handoff.get("next_step") or state.get("next_recommended_task", "check OPEN_TASKS.md")
    interrupt = handoff.get("interruption_reason", "")
    in_flight = [
        f for f in handoff.get("files_touched", [])
        if not f.startswith("=") and not f.startswith(".project-intel/")
    ][:4]
    last_cp   = handoff.get("last_checkpoint", "")
    coverage  = state.get("coverage_pct", "?")
    sessions  = state.get("total_sessions", 0)
    health    = state.get("project_health", "")

    status  = git_status(root)
    commits = git_log(root, 2)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [f"╔══ TRADE-BOT [{now}] S#{sessions} ══╗",
             f"▶ NEXT: {next_task[:160]}"]

    if interrupt:
        lines.append(f"⚠ {interrupt[:120]}")
    if in_flight:
        lines.append(f"  in-flight: {', '.join(in_flight)}")
    if last_cp:
        lines.append(f"  cp: {last_cp}")

    lines += [f"HEALTH: {health[:100]}", f"COVER: {coverage}% gate=95%", "GIT:"]
    for l in status.splitlines():
        lines.append(f"  {l}")
    for l in commits.splitlines():
        lines.append(f"  {l}")

    if impl:
        done  = sorted(k for k, v in impl.items() if str(v).lower() in ("complete", "completed"))
        other = {k: str(v)[:70] for k, v in impl.items() if k not in done}
        lines.append(f"\n✓ {', '.join(done)}")
        for k, v in other.items():
            lines.append(f"⚡ {k}: {v}")

    if decisions:
        lines.append("\nDECISIONS:")
        for d in decisions[:3]:
            lines.append(f"  • {str(d)[:95]}")
        if len(decisions) > 3:
            lines.append(f"  • (+{len(decisions)-3} in DECISION_LOG.md)")

    lines += [
        "",
        "FLOW: exchange→fetcher→storage→features→regime→models→filters→sizing→gates→executor→api",
        "GATES: DD>2%|losses≥3|volatile|pos>5%|paper<30d — KELLY×0.5 ceil 0.25",
        "",
        "RULES:",
        "  1. This IS full context. Read NO files to orient.",
        "  2. grep -n 'pattern' src/path.py — then read only lines needed.",
        "  3. find src/ -name '*.py' | xargs grep -l 'symbol'",
        "  NEVER read: MODULE_MAP.json·ARCHITECTURE.md·RAW_SCAN.json·SESSION_STATE.json·GAPS.md·rag.db",
        "  TAG: <gap><issue><broken><missing><decision><task><risk><debt><chat>",
    ]

    print("\n".join(lines))

    # Only increment session counter on real new Claude Desktop session
    if new_session:
        try:
            state["total_sessions"] = int(sessions or 0) + 1
            state["last_session_start"] = datetime.now().isoformat()
            state["session_status"] = "active"
            (intel / "SESSION_STATE.json").write_text(json.dumps(state, indent=2))
        except Exception:
            pass


if __name__ == "__main__":
    main()
